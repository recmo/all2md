import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { preview } from 'vite';
const root = mkdtempSync(join(tmpdir(), 'mdstore-spa-test-'));
writeFileSync(
  join(root, 'config.yaml'),
  'server:\n  listen: 127.0.0.1:43133\n' +
    'documents:\n  include: ["**/*.md"]\ngit:\n  push: false\nprovider:\n  api_key_env: MDSTORE_TEST_NO_KEY\n'
);
writeFileSync(
  join(root, 'rumdl.toml'),
  '[global]\nenable = ["MD012", "MD047"]\n'
);
writeFileSync(join(root, '.gitignore'), '*.mdstore\n');
writeFileSync(
  join(root, 'schema.md'),
  '# Writing guide\n\n```starlark\nmarkdown("/rumdl.toml")\n```\n'
);
writeFileSync(
  join(root, 'welcome.md'),
  '# Welcome\n\nA knowledge garden for your ideas.\n\n## Practice\n\n- Capture\n- Connect\n\n[Other note](other.md)\n'
);
writeFileSync(join(root, 'other.md'), '# Other note\n\nKeep it simple.\n');
for (const args of [
  ['init', '-q'],
  ['config', 'user.name', 'Web tests'],
  ['config', 'user.email', 'web@example.invalid'],
  ['config', 'commit.gpgsign', 'false'],
  ['add', '.'],
  ['commit', '-qm', 'Initial corpus']
])
  execFileSync('git', args, { cwd: root });
const daemon = spawn(
  fileURLToPath(new URL('../../mdstore/target/debug/mdstore', import.meta.url)),
  ['--root', root, 'serve'],
  { stdio: 'inherit' }
);
process.env.MDSTORE_URL = 'http://127.0.0.1:43133';
const ui = await preview({
  preview: { host: '127.0.0.1', port: 43132, strictPort: true }
});
const stop = () => daemon.kill('SIGINT');
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
daemon.on('exit', (code) => {
  ui.httpServer.close();
  rmSync(root, { recursive: true, force: true });
  process.exit(code || 0);
});
