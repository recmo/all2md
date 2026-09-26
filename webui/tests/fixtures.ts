import { test as base, expect } from '@playwright/test';
import { spawn } from 'node:child_process';

// Each test gets a fresh repository and daemon: submissions must not leak into
// later tests, even when the browser itself uses an isolated context.
export const test = base.extend<{ daemon: void; apiToken: string }>({
  apiToken: ['', { option: true }],
  daemon: [
    async ({ apiToken }, use) => {
      const child = spawn(process.execPath, ['tests/daemon.mjs'], {
        stdio: ['ignore', 'ignore', 'pipe'],
        env: { ...process.env, MDSTORE_TEST_TOKEN: apiToken }
      });
      let errors = '';
      child.stderr.on('data', (chunk) => {
        errors += chunk;
      });
      const exited = new Promise<void>((resolve) =>
        child.once('exit', () => resolve())
      );
      try {
        await expect
          .poll(
            async () => {
              if (child.exitCode !== null)
                throw Error(errors || 'Test daemon exited');
              return fetch('http://127.0.0.1:43132/health', {
                headers: apiToken ? { Authorization: `Bearer ${apiToken}` } : {}
              })
                .then((r) => r.ok)
                .catch(() => false);
            },
            { timeout: 15000 }
          )
          .toBe(true);
        await use();
      } finally {
        child.kill('SIGTERM');
        await exited;
      }
    },
    { auto: true }
  ]
});
export { expect };
