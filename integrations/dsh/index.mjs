import { runHostCli, withTempJsonFiles } from './rpe-bridge.mjs';

function requireHostIdentity(hostMeta) {
  if (!hostMeta || typeof hostMeta !== 'object') {
    throw new Error('trusted DSH hostMeta is required');
  }
  const { owner, sessionId, workspace, turnId = null, toolCallId = null } = hostMeta;
  if (!owner || !sessionId || !workspace) {
    throw new Error('trusted DSH hostMeta requires owner, sessionId, and workspace');
  }
  return {
    host_id: 'dsh',
    owner: String(owner),
    session_id: String(sessionId),
    workspace: String(workspace),
    ...(turnId ? { turn_id: String(turnId) } : {}),
    ...(toolCallId ? { tool_call_id: String(toolCallId) } : {}),
  };
}

/**
 * Create a thin DSH / Cordis second-host adapter bound to one RPE SQLite DB.
 *
 * Security boundary (G2):
 * - `modelTools` are exposed to the LLM turn; trusted host identity (`host_id: 'dsh'`,
 *   `owner`, `session_id`, `turn_id`, `tool_call_id`) is injected by DSH and cannot
 *   be overridden by model arguments.
 * - `trustedActions` (`confirmTask`, `cancelTask`, `reconcileTask`, `handoffTask`) are
 *   strictly host-only and MUST NEVER be registered as model-callable tools.
 */
export function createDshRpePlugin({ dbPath, pythonPath = 'python3', repoRoot } = {}) {
  if (!dbPath) {
    throw new Error('dbPath is required');
  }
  const cliOpts = { pythonPath, repoRoot };

  const modelTools = {
    async rpe_capabilities() {
      const { data } = await runHostCli(['capabilities'], cliOpts);
      return data;
    },

    async rpe_start_task(modelArgs, trustedHostMeta) {
      const context = requireHostIdentity(trustedHostMeta);
      const raw = typeof modelArgs === 'string' ? modelArgs : (modelArgs?.raw ?? modelArgs?.task);
      const draft = typeof modelArgs === 'object' && modelArgs !== null && modelArgs.draft ? modelArgs.draft : {};
      const request = { raw, draft };
      return withTempJsonFiles({ request, context }, async ({ request: reqPath, context: ctxPath }) => {
        const { data } = await runHostCli(
          ['start', '--request', reqPath, '--host-context', ctxPath, '--db', dbPath],
          cliOpts,
        );
        return data;
      });
    },

    async rpe_show_task(modelArgs, trustedHostMeta) {
      const context = requireHostIdentity(trustedHostMeta);
      const operationId = typeof modelArgs === 'string' ? modelArgs : modelArgs?.operation_id;
      const { data } = await runHostCli(
        ['show', String(operationId), '--owner', context.owner, '--db', dbPath],
        cliOpts,
      );
      return data;
    },
  };

  const trustedActions = {
    async confirmTask({ operationId, digest, generation, owner, actor = 'dsh-user', userApproved = false }) {
      const args = [
        'confirm',
        String(operationId),
        '--digest',
        String(digest),
        '--generation',
        String(generation),
        '--owner',
        String(owner),
        '--actor',
        String(actor),
        '--db',
        dbPath,
      ];
      if (userApproved === true) {
        args.push('--trusted-action');
      }
      const { data } = await runHostCli(args, cliOpts);
      return data;
    },

    async cancelTask({ operationId, generation, owner, reason }) {
      const args = [
        'cancel',
        String(operationId),
        '--generation',
        String(generation),
        '--owner',
        String(owner),
        '--db',
        dbPath,
      ];
      if (reason) {
        args.push('--reason', String(reason));
      }
      const { data } = await runHostCli(args, cliOpts);
      return data;
    },

    async reconcileTask({ operationId, owner }) {
      const { data } = await runHostCli(
        ['reconcile', String(operationId), '--owner', String(owner), '--db', dbPath],
        cliOpts,
      );
      return data;
    },

    async executeTask({ operationId, owner, confirmationId, timeout = 60 }) {
      const { data } = await runHostCli(
        [
          'execute',
          String(operationId),
          '--owner',
          String(owner),
          '--confirmation-id',
          String(confirmationId),
          '--timeout',
          String(timeout),
          '--db',
          dbPath,
        ],
        cliOpts,
      );
      return data;
    },

    async handoffTask({ sourceOperationId, sourceOwner, targetHostMeta, authorizedSources = null, pathMapping = null }) {
      const targetContext = requireHostIdentity(targetHostMeta);
      const files = { targetContext };
      if (authorizedSources !== null) files.authorizedSources = { sources: authorizedSources };
      if (pathMapping !== null) files.pathMapping = pathMapping;
      return withTempJsonFiles(files, async (paths) => {
        const args = [
          'handoff',
          String(sourceOperationId),
          '--source-owner',
          String(sourceOwner),
          '--target-context',
          paths.targetContext,
          '--db',
          dbPath,
        ];
        if (paths.authorizedSources) args.push('--authorized-sources', paths.authorizedSources);
        if (paths.pathMapping) args.push('--path-mapping', paths.pathMapping);
        const { data } = await runHostCli(args, cliOpts);
        return data;
      });
    },
  };

  return { modelTools, trustedActions };
}
