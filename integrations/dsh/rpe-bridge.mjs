import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

/**
 * Invoke `python -m rpe_harness host <subcommand>` without shell interpolation.
 */
export async function runHostCli(args, { pythonPath = 'python3', repoRoot, env = {}, timeoutMs = 15000 } = {}) {
  const fullEnv = {
    ...process.env,
    PYTHONDONTWRITEBYTECODE: '1',
    ...env,
  };
  if (repoRoot) {
    const srcDir = path.join(repoRoot, 'src');
    fullEnv.PYTHONPATH = fullEnv.PYTHONPATH ? `${srcDir}${path.delimiter}${fullEnv.PYTHONPATH}` : srcDir;
  }
  try {
    const { stdout } = await execFileAsync(pythonPath, ['-B', '-m', 'rpe_harness', 'host', ...args], {
      env: fullEnv,
      timeout: timeoutMs,
      maxBuffer: 2 * 1024 * 1024,
    });
    return { exitCode: 0, data: JSON.parse(stdout.trim()) };
  } catch (err) {
    if (err.stdout && err.stdout.trim()) {
      try {
        return { exitCode: typeof err.code === 'number' ? err.code : 1, data: JSON.parse(err.stdout.trim()) };
      } catch {
        // fall through
      }
    }
    throw new Error(`rpe host CLI failed: ${err.stderr || err.message}`);
  }
}

export async function withTempJsonFiles(fileMap, fn) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'rpe-dsh-'));
  try {
    const paths = {};
    for (const [key, value] of Object.entries(fileMap)) {
      const filePath = path.join(dir, `${key}.json`);
      await writeFile(filePath, JSON.stringify(value), 'utf8');
      paths[key] = filePath;
    }
    return await fn(paths);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}
