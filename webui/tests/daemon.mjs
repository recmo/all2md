import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  rmSync
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { preview } from 'vite';
const root = mkdtempSync(join(tmpdir(), 'mdstore-spa-test-'));
writeFileSync(
  join(root, 'config.yaml'),
  (process.env.MDSTORE_TEST_ADMIN
    ? 'server:\n  allow_template_edits: true\n  allow_config_edits: true\n  listen: 127.0.0.1:43133\n'
    : 'server:\n  listen: 127.0.0.1:43133\n') +
    (process.env.MDSTORE_TEST_TOKEN
      ? '  bearer_token_env: MDSTORE_TEST_TOKEN\n'
      : '') +
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
mkdirSync(join(root, 'notes', 'guides'), { recursive: true });
writeFileSync(
  join(root, 'notes', 'guides', 'tasks.md'),
  '# Tasks\n\n- [ ] Read about personal knowledge management\n- [x] Review the writing guide\n'
);
writeFileSync(
  join(root, 'long.md'),
  Array.from({ length: 160 }, (_, i) => 'Line ' + (i + 1)).join('\n') + '\n'
);
mkdirSync(join(root, 'reciprocal'), { recursive: true });
writeFileSync(
  join(root, 'reciprocal', 'schema.md'),
  "```starlark\nrelation('mentions', selector={'kind': 'markdown_links'}, reciprocal='mentions')\n```\n"
);
writeFileSync(join(root, 'reciprocal', 'a.md'), '[B](b.md)\n');
writeFileSync(join(root, 'reciprocal', 'b.md'), '[A](a.md)\n');
mkdirSync(join(root, 'tasks', 'v1', '2026', '09'), { recursive: true });
writeFileSync(
  join(root, 'tasks/v1/app.md'),
  readFileSync(new URL('../examples/tasks/v1/app.md', import.meta.url))
);
writeFileSync(
  join(root, 'tasks/v1/schema.md'),
  readFileSync(
    new URL('../../mdstore/examples/tasks/v1/schema.md', import.meta.url),
    'utf8'
  ) + '\n```starlark\nscope(exclude=["app.md"])\n```\n'
);
writeFileSync(
  join(root, 'tasks/v1/rumdl.toml'),
  readFileSync(
    new URL('../../mdstore/examples/tasks/v1/rumdl.toml', import.meta.url)
  )
);
writeFileSync(
  join(root, 'tasks/v1/2026/09/23-001-plan.md'),
  '---\nstate: inbox\ntags: [demo]\nstart: 2026-09-23\nend: 2026-09-25\nassignee: Remco\neffort: 3\n---\n# Plan a project\n\n## Timeline\n\n- 2026-09-23T08:00:00Z — Captured.\n'
);
writeFileSync(
  join(root, 'tasks/v1/2026/09/23-002-overlap.md'),
  '---\nstate: inbox\nstart: 2026-09-24\nend: 2026-09-26\ndepends_on: [tasks/v1/2026/09/23-001-plan.md]\nassignee: Remco\n---\n# Second overlapping task\n\n## Timeline\n\n- 2026-09-23T08:00:00Z — Captured.\n'
);
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
  {
    stdio: 'inherit',
    env: { ...process.env, MDSTORE_WORKER_TOKEN: 'browser-test-worker' }
  }
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
