import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createDshRpePlugin } from './index.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const PYTHON = process.env.RPE_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

async function withWorkspace(fn) {
  const dir = await realpath(await mkdtemp(path.join(os.tmpdir(), 'rpe-dsh-e2e-')));
  try {
    await writeFile(path.join(dir, 'input.json'), '{ "name" : [1, 2] }', 'utf8');
    await fn({ dir, dbPath: path.join(dir, 'runs.sqlite') });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test('DSH adapter enforces host identity, digest confirmation, cancellation, and cross-host handoff', async () => {
  await withWorkspace(async ({ dir, dbPath }) => {
    const plugin = createDshRpePlugin({ dbPath, pythonPath: PYTHON, repoRoot: REPO_ROOT });

    // Confirm trusted actions are NOT exposed in modelTools
    assert.equal('confirmTask' in plugin.modelTools, false);
    assert.equal('cancelTask' in plugin.modelTools, false);
    assert.equal('handoffTask' in plugin.modelTools, false);

    const pkg = JSON.parse(await readFile(path.join(__dirname, 'package.json'), 'utf8'));
    const caps = await plugin.modelTools.rpe_capabilities();
    assert.equal(caps.schema_version, 'rpe.host/v1');
    assert.equal(caps.version, pkg.version);
    assert.equal(caps.mcp_stdio, true);
    assert.ok(caps.contracts.includes('tabular.filter'));
    assert.ok(caps.contracts.includes('json.patch'));
    assert.ok(caps.methods.includes('handoff'));
    assert.ok(caps.methods.includes('execute'));

    // Model tries to spoof owner/host_id in arguments; DSH trusted hostMeta must win
    const started = await plugin.modelTools.rpe_start_task(
      {
        raw: 'Normalize input.json to out.json, minify and preserve all values.',
        owner: 'spoofed-owner',
        host_id: 'spoofed-host',
        draft: {
          capability: 'json.normalize',
          constraints: [],
          execution: { input_path: 'input.json', output_path: 'out.json', style: 'minify' },
          requirements: [
            { text: 'Normalize JSON', quote: 'Normalize', fields: ['/capability'] },
            { text: 'Read input.json', quote: 'input.json', fields: ['/execution/input_path'] },
            { text: 'Write out.json', quote: 'out.json', fields: ['/execution/output_path'] },
            { text: 'Minify', quote: 'minify', fields: ['/execution/style'] },
            { text: 'No extra constraints', quote: null, fields: ['/constraints'] },
          ],
          unresolved: [],
        },
      },
      { owner: 'dsh-alice', sessionId: 'dsh-sess-1', workspace: dir, turnId: 'turn-1', toolCallId: 'tc-1' },
    );

    assert.ok(started.operation_id);
    assert.equal(started.owner, 'dsh-alice');
    assert.equal(started.host_context.host_id, 'dsh');
    assert.equal(started.host_context.turn_id, 'turn-1');
    assert.equal(started.generation, 1);

    // Ordinary tool approval (userApproved=false) must NOT confirm
    const rejectedConfirm = await plugin.trustedActions.confirmTask({
      operationId: started.operation_id,
      digest: started.digest,
      generation: started.generation,
      owner: 'dsh-alice',
      userApproved: false,
    });
    assert.equal(rejectedConfirm.status, 'forbidden');

    // Trusted user approval confirms and bumps generation
    const confirmed = await plugin.trustedActions.confirmTask({
      operationId: started.operation_id,
      digest: started.digest,
      generation: started.generation,
      owner: 'dsh-alice',
      actor: 'audrey',
      userApproved: true,
    });
    assert.ok(confirmed.confirmation_id);
    assert.equal(confirmed.generation, 2);

    // Full end-to-end execution loop: execute confirmed task -> verify oracle output on disk
    const executed = await plugin.trustedActions.executeTask({
      operationId: started.operation_id,
      owner: 'dsh-alice',
      confirmationId: confirmed.confirmation_id,
    });
    assert.equal(executed.status, 'succeeded', JSON.stringify(executed.execution_result));
    assert.ok(executed.run_id);
    const outContent = await readFile(path.join(dir, 'out.json'), 'utf8');
    assert.equal(outContent.trim(), '{"name":[1,2]}');

    // Cross-host handoff: revoked source must be rejected
    const revokedHandoff = await plugin.trustedActions.handoffTask({
      sourceOperationId: started.operation_id,
      sourceOwner: 'dsh-alice',
      targetHostMeta: { owner: 'dsh-bob', sessionId: 'dsh-sess-2', workspace: dir },
      authorizedSources: [{ source_id: 'src-1', revoked: true, session_id: 'dsh-sess-2' }],
    });
    assert.equal(revokedHandoff.status, 'forbidden');

    // Valid cross-host handoff creates fresh operation with invalidated prior confirmation
    const handedOff = await plugin.trustedActions.handoffTask({
      sourceOperationId: started.operation_id,
      sourceOwner: 'dsh-alice',
      targetHostMeta: { owner: 'dsh-bob', sessionId: 'dsh-sess-2', workspace: dir },
      authorizedSources: [{ source_id: 'src-1', authorized: true, session_id: 'dsh-sess-2', owner: 'dsh-bob' }],
    });
    assert.ok(handedOff.operation_id);
    assert.notEqual(handedOff.operation_id, started.operation_id);
    assert.equal(handedOff.owner, 'dsh-bob');
    assert.equal(handedOff.confirmation_id, null);
    assert.equal(handedOff.handoff_lineage.handoff_from_operation_id, started.operation_id);
    assert.equal(handedOff.handoff_lineage.prior_confirmation_invalidated, true);

    // Replaying old digest on handed-off operation must return stale
    const replayed = await plugin.trustedActions.confirmTask({
      operationId: handedOff.operation_id,
      digest: started.digest,
      generation: 1,
      owner: 'dsh-bob',
      userApproved: true,
    });
    assert.equal(replayed.status, 'stale');

    // Cancel and reconcile cleanly
    const cancelled = await plugin.trustedActions.cancelTask({
      operationId: handedOff.operation_id,
      generation: 1,
      owner: 'dsh-bob',
      reason: 'user stopped from DSH',
    });
    assert.equal(cancelled.status, 'cancelled');

    const reconciled = await plugin.trustedActions.reconcileTask({
      operationId: handedOff.operation_id,
      owner: 'dsh-bob',
    });
    assert.equal(reconciled.terminal, true);
    assert.equal(reconciled.terminal_state, 'cancelled');
  });
});
