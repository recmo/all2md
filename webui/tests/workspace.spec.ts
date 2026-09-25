import { type Page } from '@playwright/test';
import { test, expect } from './fixtures';

test('language fences retain highlighting through preview, editing and offline reload', async ({
  page,
  context
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  const samples = [
    ['starlark', 'markdown("rumdl.toml")', 'rumdl.toml'],
    ['rust', 'fn main() {}', 'fn'],
    ['latex', '\\section{Hello}\n% comment', '% comment'],
    ['typst', '#let x = "hello"', 'hello'],
    ['lean4', 'theorem test : True := by trivial', 'theorem']
  ];
  await replaceDocument(
    page,
    samples
      .map(([lang, code]) => '```' + lang + '\n' + code + '\n```')
      .join('\n\n') + '\n'
  );
  const starlarkToken = page
    .locator('.code-editor [data-line] span[style]')
    .filter({ hasText: /rumdl\.toml/ });
  await expect(starlarkToken).toBeVisible();
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  for (const [lang, , token] of samples) {
    const language = lang === 'latex' ? 'tex' : lang;
    await expect(
      page
        .locator('.markdown-code[data-language="' + language + '"] span[style]')
        .filter({ hasText: token })
        .first()
    ).toBeVisible();
  }
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  for (const [lang, , token] of samples) {
    const language = lang === 'latex' ? 'tex' : lang;
    await expect(
      page
        .locator('.markdown-code[data-language="' + language + '"] span[style]')
        .filter({ hasText: token })
        .first()
    ).toBeVisible();
  }
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await expect(starlarkToken).toBeVisible();
  await expect(page.locator('.code-editor')).toContainText('```starlark');
});

async function replaceDocument(page: Page, text: string) {
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  const editor = page.locator('.code-editor [role=textbox]');
  await editor.focus();
  await editor.press('ControlOrMeta+a');
  await page.keyboard.insertText(text);
}

test('offline editing survives reload and search uses the MCP API online', async ({ page, context }) => {
  const calls: string[] = [];
  page.on('request', r => { if (r.url().endsWith('/mcp')) calls.push(r.postDataJSON().params.name); });
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'welcome.md', exact: true }).click();
  await replaceDocument(page, '# Welcome\n\nOffline knowledge garden.\n');
  await expect(page.locator('.document-header')).toContainText('Valid');
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator('#preview')).toContainText('Offline knowledge garden.');
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').fill('garden');
  await page.locator('#search').press('Enter');
  await expect(page.getByText('OFFLINE · CACHED TEXT')).toBeVisible();
  await context.setOffline(false);
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: /^Connected$/ })).toBeVisible();
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').press('Enter');
  await expect(page.getByText(/MCP RESULTS/)).toBeVisible();
  expect(calls).toContain('search');
  expect(calls).toContain('get_page');
});

test('connection settings are separate and keep the token in memory across SPA navigation', async ({
  page
}) => {
  await page.goto('/settings');
  await expect(
    page.getByRole('heading', { name: 'Settings', exact: true })
  ).toBeVisible();
  await page.getByLabel('Bearer token').fill('test-memory-token');
  const listing = page.waitForRequest((r) => r.url().endsWith('/mcp') && r.postDataJSON()?.params?.name === 'get_page' && r.postDataJSON()?.params?.arguments?.path === '/' && r.headers()['authorization'] === 'Bearer test-memory-token');
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Connected.' })).toBeVisible();
  expect((await listing).headers()['authorization']).toBe(
    'Bearer test-memory-token'
  );
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.getByLabel('Bearer token')).toHaveCount(0);
  await expect(page.locator('#preview h1')).toHaveText('Other note');
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    'test-memory-token'
  );
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await expect(page).toHaveURL('/#@settings');
  await expect(
    page.getByRole('heading', { name: 'Settings', exact: true })
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole('heading', { name: 'Settings', exact: true })
  ).toBeVisible();
  await expect(page.getByLabel('Bearer token')).toHaveValue('');
});

test('configuration is read-only YAML and template fences are highlighted', async ({
  page
}) => {
  await page.goto('/');
  await page
    .getByRole('treeitem', { name: 'config.yaml', exact: true })
    .click();
  await expect(page.locator('.code-editor')).toContainText('documents:');
  await expect(page.locator('.code-editor [contenteditable=true]')).toHaveCount(
    0
  );
  await page.locator('[data-item-path="template.md"]').click();
  await expect(
    page
      .locator('#preview .markdown-code span')
      .filter({ hasText: /rumdl\.toml/ })
      .first()
  ).toContainText('rumdl.toml');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await page.getByRole('link', { name: 'template.md', exact: true }).click();
  await expect(
    page
      .locator('#preview .markdown-code span')
      .filter({ hasText: /rumdl\.toml/ })
      .first()
  ).toContainText('rumdl.toml');
});

test('folder hierarchy opens documents with rendered task lists', async ({
  page
}) => {
  await page.goto('/');
  const notes = page.getByRole('treeitem', {
    name: 'notes / guides',
    exact: true
  });
  const tasks = page.getByRole('treeitem', { name: 'tasks.md', exact: true });
  await expect(tasks).toBeVisible();
  await notes.focus();
  await notes.press('ArrowLeft');
  await expect(tasks).toBeHidden();
  await notes.press('ArrowRight');
  await expect(tasks).toBeVisible();
  await tasks.focus();
  await tasks.press('Enter');
  await expect(page.locator('.document-breadcrumb')).toHaveText(/notes\s*\/\s*guides\s*\/\s*tasks.md/);
  const unchecked = page.getByRole('checkbox', { name: 'Incomplete task' });
  const checked = page.getByRole('checkbox', { name: 'Completed task' });
  await expect(unchecked).not.toBeChecked();
  await expect(checked).toBeChecked();
  await expect(unchecked).toBeDisabled();
  await expect(page.locator('#preview')).not.toContainText('[ ]');
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await expect(page.locator('.code-editor [role=textbox]')).toContainText(
    '- [ ] Read about'
  );
});

test('switching rendered and code preserves changes and undo history', async ({
  page
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Replacement\n\nText.\n');
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await expect(page.locator('#preview h1')).toHaveText('Replacement');
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  const editor = page.locator('.code-editor [role=textbox]');
  await editor.focus();
  await editor.press('ControlOrMeta+z');
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await expect(page.locator('#preview h1')).toHaveText('Other note');
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await editor.focus();
  await editor.press('ControlOrMeta+Shift+z');
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await expect(page.locator('#preview h1')).toHaveText('Replacement');
});

test('new empty documents can be edited and discarded without stale content', async ({
  page
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'notes / guides', exact: true }).click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'New document here…' }).click();
  await page.getByLabel('New document path').fill('notes/new.md');
  await page.getByRole('button', { name: 'Create draft', exact: true }).click();
  await expect(page.locator('.document-breadcrumb')).toHaveText(/notes\s*\/\s*new.md/);
  await replaceDocument(
    page,
    '# New draft\n\n~~~unknown\n<script>literal</script>\n~~~\n'
  );
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await expect(page.locator('#preview h1')).toHaveText('New draft');
  await expect(page.locator('#preview .markdown-code')).toContainText(
    '<script>literal</script>'
  );
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('treeitem', { name: 'new.md', exact: true }).click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
  await expect(page.getByRole('treeitem', { name: 'new.md', exact: true })).toHaveCount(0);
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await expect(page.locator('.code-editor')).not.toContainText('New draft');
});

test('tree offline badges update and folder menus create drafts in place', async ({
  page,
  context
}) => {
  await page.goto('/');
  const document = page.getByRole('treeitem', {
    name: 'tasks.md',
    exact: true
  });
  await expect(
    document.getByTitle('Available offline — cached copy, may differ from the server')
  ).toBeVisible();
  await expect(
    page
      .getByRole('treeitem', { name: 'notes / guides', exact: true })
      .getByTitle(
        '1 of 1 documents available offline; 0 local drafts awaiting submission'
      )
  ).toBeVisible();
  await document.click();
  await expect(
    document.getByTitle(
      'Available offline — cached copy, may differ from the server'
    )
  ).toBeVisible();
  await replaceDocument(page, '# Cached draft\n');
  await expect(
    document.getByTitle(
      'Local draft — available offline; awaiting validation and submission'
    )
  ).toBeVisible();
  await page
    .getByRole('treeitem', { name: 'notes / guides', exact: true })
    .click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'New document here…' }).click();
  await expect(page.getByLabel('New document path')).toHaveValue(
    'notes/guides/'
  );
  await page.getByLabel('New document path').fill('notes/guides/new-here.md');
  await page.getByRole('button', { name: 'Create draft', exact: true }).click();
  await expect(page.locator('.document-breadcrumb')).toHaveText(/notes\s*\/\s*guides\s*\/\s*new-here.md/);
  await expect(
    page
      .getByRole('treeitem', { name: 'new-here.md', exact: true })
      .getByTitle(
        'Local draft — available offline; awaiting validation and submission'
      )
  ).toBeVisible();
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  await expect(
    page
      .getByRole('treeitem', { name: 'new-here.md', exact: true })
      .getByTitle(
        'Local draft — available offline; awaiting validation and submission'
      )
  ).toBeVisible();
});

test('CodeView retains its full scroll range across rendered/code switches', async ({
  page
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'long.md', exact: true }).click();
  const view = page.locator('.code-editor');
  const pane = page.locator('main.document-main');
  for (let i = 0; i < 3; i++) {
    await page.getByRole('radio', { name: 'Edit', exact: true }).click();
    await expect(view).toBeVisible();
    await pane.evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await expect(
      view.locator('[data-line]').filter({ hasText: /^Line 160$/ })
    ).toBeVisible();
    expect(
      await page
        .locator('.document-header')
        .evaluate((el) => el.getBoundingClientRect().bottom)
    ).toBeLessThan(0);
    expect(
      await view.evaluate((el) => el.scrollHeight - el.clientHeight)
    ).toBeLessThanOrEqual(1);
    await pane.evaluate((el) => {
      el.scrollTop = 0;
    });
    await page.getByRole('radio', { name: 'Render', exact: true }).click();
    await expect(view).toBeHidden();
    await expect(page.locator('#preview')).toContainText('Line 160');
  }
});

test('validation does not require a commit description, but submission does', async ({
  page
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\nValidate before describing.\n');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('#summary')).toHaveValue('');
  await expect(page.locator('.validation-results')).toContainText('Validation passed.');
  await expect(page.locator('#submit')).toBeDisabled();
  await page.locator('#summary').fill('Describe the already validated change');
  await expect(page.locator('#submit')).toBeEnabled();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\n[Broken](missing.md)\n');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('#submit')).toBeDisabled();
});

test('validation findings navigate to inline editor annotations and clear after edits', async ({
  page
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\n[Broken](missing-target.md)\n');
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  const errors = page.getByRole('list', { name: 'Validation errors' });
  await expect(errors).toContainText('other.md:3');
  await errors.getByRole('button').first().click();
  const diagnostic = page.locator('.code-editor .validation-diagnostic');
  await expect(diagnostic).toBeVisible();
  await expect(diagnostic).toContainText('dangling internal target');
  await expect(page.locator('.code-editor [data-line="3"]')).toContainText(
    'Broken'
  );
  await replaceDocument(page, '# Other note\n\nFixed.\n');
  await expect(diagnostic).toHaveCount(0);
  await expect(errors).toHaveCount(0);
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('.validation-results')).toContainText('Validation passed');
});

test('privileged configuration and template edits validate in the editor', async ({
  page
}) => {
  test.skip(!process.env.MDSTORE_TEST_ADMIN, 'Requires privileged test daemon');
  await page.goto('/');
  await page.locator('[data-item-path="template.md"]').click();
  await replaceDocument(page, '```starlark\nunknown_rule()\n```\n');
  await expect(page.locator('.validation-diagnostic')).toContainText(
    'invalid template'
  );
  await replaceDocument(
    page,
    '# Updated guide\n\n```starlark\nmarkdown("rumdl.toml")\n```\n'
  );
  await page
    .getByRole('treeitem', { name: 'config.yaml', exact: true })
    .click();
  const editor = page.locator('.code-editor [role=textbox]');
  await expect(editor).toBeVisible();
  await editor.focus();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText('search:\n  limit: 0\n');
  await expect(page.locator('.validation-diagnostic')).toContainText(
    'invalid configuration'
  );
  await editor.focus();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText(
    'documents:\n  include: ["**/*.md"]\ngit:\n  push: false\nserver:\n  listen: 127.0.0.1:43133\n  allow_template_edits: true\n  allow_config_edits: true\nprovider:\n  api_key_env: MDSTORE_TEST_NO_KEY\n'
  );
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await page.locator('#summary').fill('Update template and configuration');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('.validation-results')).toContainText('Validation passed');
  await page.locator('#submit').click();
  await expect(page.locator('.submit-notice')).toContainText('Changes committed successfully');
  await page.reload();
  await page.locator('[data-item-path="template.md"]').click();
  await expect(page.locator('#preview')).toContainText('Updated guide');
});

test('WASM validates offline without fetching unchanged document source or calling the API', async ({
  page,
  context
}) => {
  let serverValidations = 0;
  await page.route('**/validate', async (route) => {
    serverValidations++;
    await route.abort();
  });
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other\n\nCurrent edit.\n');
  await expect(page.locator('.document-status')).toContainText('Valid');
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  await replaceDocument(page, '# Other\n\n[Broken](no-such-page.md)\n');
  await expect(page.locator('.validation-diagnostic')).toContainText(
    'dangling internal target'
  );
  await replaceDocument(page, '# Other\n\nFixed offline.\n');
  await expect(page.locator('.document-status')).toContainText('Valid');
  expect(serverValidations).toBe(0);
});

test('WASM catches an unchanged document losing its reciprocal link without fetching its source', async ({
  page,
  context
}) => {
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(url: string | URL, options?: WorkerOptions) {
        super(url, options);
        this.addEventListener('message', (event) => {
          (window as any).validationResult = event.data.result;
        });
      }
    };
  });
  let fetchedB = false;
  await page.route('**/mcp', async (route) => {
    const request = route.request().postDataJSON();
    if (
      request.params?.name === 'get_page' &&
      request.params.arguments.path === 'reciprocal/b.md'
    )
      fetchedB = true;
    await route.continue();
  });
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'a.md', exact: true }).click();
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  fetchedB = false; // Initial MCP traversal cached all published sources.
  await context.setOffline(true);
  await replaceDocument(page, '# Removed link\n');
  await expect
    .poll(() => page.evaluate(() => (window as any).validationResult?.findings))
    .toEqual([
      expect.objectContaining({
        path: 'reciprocal/b.md',
        message: expect.stringContaining('missing reciprocal')
      })
    ]);
  await replaceDocument(page, '[B](b.md)\nMore text.\n');
  await expect(page.locator('.document-status')).toContainText('Valid');
  expect(fetchedB).toBe(false);
});

test('WASM template changes validate the cached corpus offline', async ({
  page,
  context
}) => {
  test.skip(!process.env.MDSTORE_TEST_ADMIN, 'Requires privileged test daemon');
  let fetchedB = false;
  await page.route('**/mcp', async (route) => {
    const request = route.request().postDataJSON();
    if (
      request.params?.name === 'get_page' &&
      request.params.arguments.path === 'reciprocal/b.md'
    )
      fetchedB = true;
    await route.continue();
  });
  await page.goto('/');
  await page.locator('[data-item-path="reciprocal/template.md"]').click();
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  fetchedB = false; // Initial MCP traversal cached all published sources.
  await context.setOffline(true);
  await replaceDocument(
    page,
    '```starlark\nfrontmatter(name=string(required=True))\n```\n'
  );
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('.validation-results')).toContainText('Validation failed');
  expect(fetchedB).toBe(false);
  await context.setOffline(false);
  await expect(
    page.getByRole('list', { name: 'Validation errors' })
  ).toContainText('reciprocal/b.md');
  expect(fetchedB).toBe(false);
});

test('inline diagnostics remain visible until background validation replaces them', async ({
  page
}) => {
  await page.addInitScript(() => {
    const control = { hold: false, pending: [] as (() => void)[] };
    (window as any).validationControl = control;
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      postMessage(message: any) {
        if (control.hold)
          control.pending.push(() => super.postMessage(message));
        else super.postMessage(message);
      }
    };
  });
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Note\n\n[Missing](first-missing.md)\n');
  const diagnostics = page.locator('.validation-diagnostic');
  await expect(diagnostics).toContainText('first-missing.md');

  for (const next of ['second-missing.md', null]) {
    await page.evaluate(() => {
      (window as any).validationControl.hold = true;
    });
    const previous = await diagnostics.innerText();
    await replaceDocument(
      page,
      next ? `# Note\n\n[Missing](${next})\n` : '# Note\n\nValid text.\n'
    );
    // The old result survives both the debounce and the in-flight worker job.
    await expect(diagnostics).toHaveText(previous);
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).validationControl.pending.length)
      )
      .toBe(1);
    await expect(diagnostics).toHaveText(previous);
    await page.evaluate(() => {
      const control = (window as any).validationControl;
      control.hold = false;
      control.pending.splice(0).forEach((send: () => void) => send());
    });
    if (next) await expect(diagnostics).toContainText(next);
    else await expect(diagnostics).toHaveCount(0);
  }
  await expect(page.locator('.document-status')).toContainText('Valid');
});

test('rumdl reports the same blank-line finding in WASM offline and on the server', async ({
  page,
  context
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  const base = '# Other note\n\nKeep it simple.\n';
  const text = '# Other note\n\nKeep it simple.\n\n\nMore.\n';
  const response = await page.request.post('/mcp', {
    data: { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'apply_edits', arguments: {
      edit_summary: 'Check rumdl', edits: [{ op: 'replace_page', path: 'other.md', base, content: text }]
    } } }
  });
  const result = (await response.json()).result;
  expect(result.isError).toBe(true);
  const findings = result.structuredContent.validation_findings;
  expect(findings).toEqual([
    expect.objectContaining({
      path: 'other.md',
      line: 5,
      message: expect.stringMatching(/^MD012:/)
    })
  ]);
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await replaceDocument(page, text);
  await expect(page.locator('.validation-diagnostic')).toHaveText(
    findings[0].message
  );
  await replaceDocument(page, '# Other note\n\nKeep it simple.\n\nMore.\n');
  await expect(page.locator('.validation-diagnostic')).toHaveCount(0);
  await expect(page.locator('.document-status')).toContainText('Valid');
});

test('tree folders and moves persist offline and stage rewritten backlinks', async ({ page, context }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await page.locator('.document-tree-host').click({ button: 'right', position: { x: 200, y: 700 } });
  await page.getByRole('menuitem', { name: 'New folder here…', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill('archive');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('treeitem', { name: 'archive', exact: true })).toBeVisible();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Move…', exact: true }).click();
  await page.getByLabel('Destination path').fill('archive/other.md');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('dialog')).not.toBeVisible();
  await expect(page.locator('[data-item-path="archive/other.md"]')).toBeVisible();
  const staged = await page.evaluate(() => {
    const key = Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!;
    return JSON.parse(localStorage.getItem(key)!);
  });
  expect(staged.deletions['other.md']).toContain('# Other note');
  expect(staged.drafts['welcome.md'].text).toContain('(archive/other.md)');
  await expect(page.locator('.document-header')).toContainText('Valid');
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  await page.locator('[data-item-path="archive/other.md"]').click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Rename…', exact: true }).click();
  await page.getByRole('textbox', { name: 'Rename other.md', exact: true }).fill('renamed.md');
  await page.getByRole('textbox', { name: 'Rename other.md', exact: true }).press('Enter');
  await expect(page.locator('[data-item-path="archive/renamed.md"]')).toBeVisible();
  await expect(page.locator('.document-header')).toContainText('Valid');
  await page.reload();
  await expect(page.locator('[data-item-path="archive/renamed.md"]')).toBeVisible();
});

test('drag moves and double-click renames stage backlink updates', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await page.locator('.document-tree-host').click({ button: 'right', position: { x: 200, y: 700 } });
  await page.getByRole('menuitem', { name: 'New folder here…', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill('archive');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('treeitem', { name: 'archive', exact: true }).dblclick();
  await page.getByRole('textbox', { name: 'Rename archive', exact: true }).fill('cancelled');
  await page.getByRole('textbox', { name: 'Rename archive', exact: true }).press('Escape');
  await expect(page.getByRole('treeitem', { name: 'archive', exact: true })).toBeVisible();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).dragTo(page.getByRole('treeitem', { name: 'archive', exact: true }));
  await expect(page.locator('[data-item-path="archive/other.md"]')).toBeVisible();
  await page.locator('[data-item-path="archive/other.md"]').dblclick();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('textbox', { name: 'Rename other.md', exact: true }).fill('renamed.md');
  await page.getByRole('textbox', { name: 'Rename other.md', exact: true }).press('Enter');
  await expect(page.locator('[data-item-path="archive/renamed.md"]')).toBeVisible();
  await expect(page.locator('.document-header')).toContainText('Valid');
  const staged = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!)!));
  expect(staged.drafts['welcome.md'].text).toContain('(archive/renamed.md)');
  expect(staged.deletions['other.md']).toContain('# Other note');
  // Protected files must be restored after the tree's optimistic drop.
  await page.getByRole('treeitem', { name: 'config.yaml', exact: true }).dragTo(page.getByRole('treeitem', { name: 'archive', exact: true }));
  await expect(page.getByRole('alert')).toContainText('not permitted');
  await expect(page.locator('[data-item-path="config.yaml"]')).toBeVisible();
  await expect(page.locator('[data-item-path="archive/config.yaml"]')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('[data-item-path="archive/renamed.md"]')).toBeVisible();
  await page.locator('.document-tree-host').click({ button: 'right', position: { x: 200, y: 700 } });
  await page.getByRole('menuitem', { name: 'New folder here…', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill('box');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('treeitem', { name: 'archive', exact: true }).dragTo(page.getByRole('treeitem', { name: 'box', exact: true }));
  await expect(page.locator('[data-item-path="box/archive/renamed.md"]')).toBeVisible();
  await expect(page.locator('.document-header')).toContainText('Valid');
  const movedFolder = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!)!));
  expect(movedFolder.drafts['welcome.md'].text).toContain('(box/archive/renamed.md)');

});

test('sidebar contains only trees with Search and Settings pages', async ({ page, context }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.locator('aside input')).toHaveCount(0);
  await expect(page.locator('aside footer')).toHaveCount(0);
  await expect(page.locator('aside')).not.toContainText('YOUR KNOWLEDGE');
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Search', exact: true })).toBeVisible();
  await page.locator('#search').fill('garden');
  const mcp = page.waitForRequest(request => request.url().endsWith('/mcp') && request.postDataJSON()?.params?.name === 'search');
  await page.locator('#search').press('Enter');
  await mcp;
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await expect(page.getByLabel('Bearer token')).toBeVisible();
  await page.getByRole('button', { name: /Make library available offline/ }).click();
  await expect(page.getByRole('button', { name: /Make library available offline/ })).toBeEnabled();
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').fill('garden');
  await page.locator('#search').press('Enter');
  await expect(page.locator('.search-results')).toContainText('welcome.md');
  await expect(page.getByRole('treeitem', { name: 'other.md', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Search', exact: true })).toBeVisible();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.locator('#preview')).toContainText('Other note');
  const navigation = await page.locator('.workspace-navigation').boundingBox();
  for (const name of ['Search', 'Settings', 'Submit']) {
    const row = await page.getByRole('treeitem', { name, exact: true }).boundingBox();
    expect(row!.y).toBeGreaterThanOrEqual(navigation!.y);
    expect(row!.y + row!.height).toBeLessThanOrEqual(navigation!.y + navigation!.height);
  }
  await page.screenshot({ path: '/tmp/mdstore-tree-sidebar.png' });
});

test('submit pane reviews validation and diffs, then commits the full staged batch', async ({ page, context }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\n[Missing](missing-submit-target.md)\n');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Submit', exact: true })).toBeVisible();
  await page.getByLabel('Description').fill('Review and reorganize notes');
  await expect(page.getByRole('list', { name: 'Validation errors' })).toContainText('missing-submit-target');
  await expect(page.locator('#submit')).toBeDisabled();
  await page.getByRole('list', { name: 'Validation errors' }).getByRole('button').first().click();
  await replaceDocument(page, '# Other note\n\nReviewed in the submit pane.\n');
  await page.getByRole('treeitem', { name: 'welcome.md', exact: true }).dblclick();
  await page.getByRole('textbox', { name: 'Rename welcome.md', exact: true }).fill('welcome-submitted.md');
  await page.getByRole('textbox', { name: 'Rename welcome.md', exact: true }).press('Enter');
  await expect(page.getByRole('treeitem', { name: 'welcome-submitted.md', exact: true })).toBeVisible();
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('#submit')).toBeEnabled();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('Added');
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('Deleted');
  await expect(page.locator('.staged-diff').first()).toBeVisible();
  await expect(page.locator('.staged-file').filter({ hasText: 'other.md' }).locator('[data-line]').filter({ hasText: 'Reviewed in the submit pane.' })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Description')).toHaveValue('Review and reorganize notes');
  await context.setOffline(true);
  await expect(page.locator('#submit')).toBeDisabled();
  await context.setOffline(false);
  await expect(page.locator('#submit')).toBeEnabled();
  await page.screenshot({ path: '/tmp/mdstore-submit-pane.png' });
  const applied = page.waitForRequest(request => request.url().endsWith('/mcp') && request.postDataJSON()?.params?.name === 'apply_edits');
  await page.locator('#submit').click();
  expect((await applied).postDataJSON().params.arguments.edits).toHaveLength(3);
  await expect(page.getByRole('status').filter({ hasText: 'Changes committed successfully.' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('no staged changes');
  const listing = await directoryPaths(page, '/');
  expect(listing).toContain('welcome-submitted.md');
  expect(listing).not.toContain('welcome.md');
  const cache = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!)!));
  expect(cache.drafts).toEqual({});
  expect(cache.deletions).toEqual({});
  expect(cache.pages['other.md'].text).toContain('Reviewed in the submit pane.');
  expect(cache.summary).toBe('');
});

test('server rejection preserves staged edits and description', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\nKeep this local draft.\n');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await page.getByLabel('Description').fill('Preserve rejected submission');
  await expect(page.locator('#submit')).toBeEnabled();
  await page.route('**/mcp', async route => {
    const body = route.request().postDataJSON();
    if (body?.params?.name !== 'apply_edits') return route.continue();
    await route.fulfill({ json: { jsonrpc: '2.0', id: body.id, error: { code: -32000, message: 'The document changed on the server.' } } });
  });
  await page.locator('#submit').click();
  await expect(page.getByRole('alert')).toContainText('Your staged changes are preserved');
  await expect(page.getByLabel('Description')).toHaveValue('Preserve rejected submission');
  await expect(page.locator('.staged-file')).toContainText('other.md');
  await page.reload();
  await expect(page.getByLabel('Description')).toHaveValue('Preserve rejected submission');
  await expect(page.locator('.staged-file')).toContainText('other.md');
});

test('file and folder deletion is staged, validated, undoable, and survives reload', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '# Other note\n\nPreserve this draft on undo.\n');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Submit', exact: true })).toBeVisible();
  await expect(page.locator('[data-item-path="other.md"] [data-item-section="content"]')).toHaveCSS('text-decoration-line', 'line-through');
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('Deleted');
  await expect(page.getByRole('list', { name: 'Validation errors' })).toContainText('dangling internal target');
  await page.getByRole('button', { name: 'Undo deletion', exact: true }).click();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.locator('.code-editor')).toContainText('Preserve this draft on undo.');
  await page.getByRole('treeitem', { name: 'notes / guides', exact: true }).click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
  await expect(page.locator('[data-item-path="notes/guides/tasks.md"] [data-item-section="content"]')).toHaveCSS('text-decoration-line', 'line-through');
  await page.getByLabel('Description').fill('Delete example folder');
  await expect(page.locator('#submit')).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('notes/guides/tasks.md');
  await expect(page.locator('[data-item-path="notes/guides/tasks.md"] [data-item-section="content"]')).toHaveCSS('text-decoration-line', 'line-through');
  const cache = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!)!));
  expect(cache.deletions['notes/guides/tasks.md']).toContain('# Tasks');
  expect(cache.drafts['other.md'].text).toContain('Preserve this draft on undo.');
  const listing = await directoryPaths(page, 'notes/guides/');
  expect(listing).toContain('notes/guides/tasks.md');
});

test('long tree names preserve badges and display validation and deletion states', async ({ page }) => {
  await page.goto('/');
  const name = 'a-very-long-task-file-name-that-must-leave-room-for-validation-and-sync-status.md';
  const path = 'notes/guides/' + name;
  await page.getByRole('treeitem', { name: 'tasks.md', exact: true }).dblclick();
  const input = page.getByRole('textbox', { name: 'Rename tasks.md', exact: true });
  await input.fill(name);
  await input.press('Enter');
  const row = page.locator(`[data-item-path="${path}"]`);
  await expect(row.locator('[data-item-section="decoration"] [title]')).toHaveAttribute('title', /; Valid$/);
  const bounds = await row.boundingBox();
  const badge = await row.locator('[data-item-section="decoration"]').boundingBox();
  const content = await row.locator('[data-item-section="content"]').boundingBox();
  expect(badge!.width).toBeGreaterThan(15);
  expect(badge!.x + badge!.width).toBeLessThanOrEqual(bounds!.x + bounds!.width);
  expect(content!.x + content!.width).toBeLessThanOrEqual(badge!.x);
  await expect(page.locator('[data-item-path="notes/guides/tasks.md"] [data-item-section="content"]')).toHaveCSS('text-decoration-line', 'line-through');
  await row.click();
  await replaceDocument(page, '# Task\n\n[Missing](missing-badge-target.md)\n');
  await expect(row.locator('[data-item-section="decoration"] [title]')).toHaveAttribute('title', /Invalid/);
  await page.screenshot({ path: '/tmp/mdstore-tree-status.png' });
  await page.locator('[data-item-path="notes/guides/tasks.md"]').click();
  await expect(page.getByRole('heading', { name: 'Submit', exact: true })).toBeVisible();
});

test('mobile navigation overlays content, preserves the tree, and closes accessibly', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const drawer = page.locator('#navigation-sidebar');
  const toggle = page.getByRole('button', { name: 'Open navigation', exact: true });
  await expect(drawer).not.toBeVisible();
  const content = await page.locator('main').boundingBox();
  expect(content!.x).toBe(0);
  await toggle.click();
  await expect(drawer).toBeVisible();
  await expect(drawer).toHaveAttribute('aria-modal', 'true');
  await expect(page.locator('main')).toHaveAttribute('inert', '');
  const tree = page.locator('#documents file-tree-container');
  await expect.poll(async () => (await tree.boundingBox())!.height).toBeGreaterThan(400);
  await page.screenshot({ path: '/tmp/mdstore-mobile-drawer.png' });
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.locator('#preview')).toContainText('Other note');
  await expect(drawer).not.toBeVisible();
  await expect(toggle).toBeFocused();
  await toggle.click();
  // The same native tree survives closing, including its expansion state.
  await tree.evaluate(el => { el.setAttribute('data-preserved', 'yes'); });
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible();
  await toggle.click();
  await expect(tree).toHaveAttribute('data-preserved', 'yes');
  await expect(drawer.getByRole('button', { name: 'Close navigation', exact: true })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  expect(await drawer.evaluate(el => el.contains(document.activeElement))).toBe(true);
  await page.locator('.drawer-backdrop').click({ position: { x: 380, y: 400 } });
  await expect(drawer).not.toBeVisible();
  await toggle.click();
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await expect(drawer).not.toBeVisible();
  await expect(page.getByRole('heading', { name: 'Search', exact: true })).toBeVisible();
  await toggle.click();
  await drawer.evaluate(el => {
    const start = new Touch({ identifier: 1, target: el, clientX: 250, clientY: 100 });
    const end = new Touch({ identifier: 1, target: el, clientX: 100, clientY: 110 });
    el.dispatchEvent(new TouchEvent('touchstart', { touches: [start] }));
    el.dispatchEvent(new TouchEvent('touchend', { changedTouches: [end] }));
  });
  await expect(drawer).not.toBeVisible();
  await toggle.click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(drawer).toBeVisible();
  await expect(page.locator('main')).not.toHaveAttribute('inert');
  await expect(toggle).not.toBeVisible();
  expect(await page.evaluate(() => document.body.style.position)).toBe('');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(drawer).not.toBeVisible();
});

test('frontmatter renders compact properties on mobile and preserves YAML in edit mode', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(page, '---\ntitle: Other note\nstate: inbox\nwaiting_on: null\ntags: [demo, example]\nowner: Remco\n---\n\n# Other note\n\nBody.\n');
  await page.getByRole('radio', { name: 'Render', exact: true }).click();
  await expect(page.locator('#preview h1')).toHaveText('Other note');
  await expect(page.locator('#preview .property-badges')).toContainText('inbox');
  await expect(page.locator('#preview')).not.toContainText('waiting_on');
  await expect(page.locator('#preview .markdown-code')).toHaveCount(0);
  await page.locator('#preview summary').click();
  await expect(page.locator('#preview dd')).toHaveText('Remco');
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await expect(page.locator('.code-editor')).toContainText('waiting_on: null');
});

async function remoteEdit(page: Page, path: string, content: string, base?: string) {
  const response = await page.request.post('/mcp', { data: {
    jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'apply_edits', arguments: {
      edit_summary: 'Concurrent client edit', edits: [base === undefined ? { op: 'create_page', path, content } : { op: 'replace_page', path, base, content }]
    } }
  } });
  const value = await response.json();
  expect(value.error).toBeUndefined();
  expect(value.result.isError).not.toBe(true);
}

test('reconciliation merges remote changes before validated submission', async ({ page }) => {
  const path = 'merge-clean.md';
  const base = '# Original\n\nShared paragraph.\n\nOriginal ending.\n';
  await remoteEdit(page, path, base);
  await page.goto('/#' + path);
  await expect(page.locator('#preview')).toContainText('Original ending.');
  await replaceDocument(page, base.replace('Original ending.', 'Local ending.'));
  await remoteEdit(page, path, base.replace('# Original', '# Remote heading'), base);
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await page.getByLabel('Description').fill('Merged changes');
  await expect(page.locator('#submit')).toBeEnabled();
  const staged = page.getByRole('region', { name: 'Staged diff' });
  await expect(staged).toContainText('Local ending.');
  const applied = page.waitForRequest(r => r.url().endsWith('/mcp') && r.postDataJSON()?.params?.name === 'apply_edits');
  await page.locator('#submit').click();
  const edit = (await applied).postDataJSON().params.arguments.edits[0];
  expect(edit.base).toContain('# Remote heading');
  expect(edit.content).toContain('# Remote heading');
  expect(edit.content).toContain('Local ending.');
  await expect(page.getByRole('status').filter({ hasText: 'Changes committed successfully.' })).toBeVisible();
});

test('conflicts persist offline, resolve per passage, and detect another server edit', async ({ page, context }) => {
  const path = 'merge-conflict.md';
  const base = '# Conflict\n\nOriginal passage.\n\nShared ending.\n';
  const theirs = base.replace('Original passage.', 'Server passage.');
  await remoteEdit(page, path, base);
  await page.goto('/#' + path);
  await expect(page.locator('#preview')).toContainText('Original passage.');
  await replaceDocument(page, base.replace('Original passage.', 'Local passage.').replace('Shared ending.', 'Local ending.'));
  await remoteEdit(page, path, theirs, base);
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  const conflict = page.getByRole('region', { name: 'Conflict in ' + path, exact: true });
  await expect(conflict).toBeVisible();
  await expect(page.locator('#submit')).toBeDisabled();
  await conflict.getByRole('button', { name: 'Edit manually', exact: true }).click();
  await conflict.getByLabel('Resolved passage').fill('Combined passage.\n');
  await page.reload();
  await expect(conflict).toContainText('Combined passage.');
  await context.setOffline(true);
  await conflict.getByRole('button', { name: 'Apply resolution', exact: true }).click();
  await expect(conflict).toHaveCount(0);
  await page.getByLabel('Description').fill('Resolve conflicting edits');
  await context.setOffline(false);
  await expect(page.locator('#submit')).toBeEnabled();
  const newer = theirs.replace('Server passage.', 'Newer server passage.');
  await remoteEdit(page, path, newer, theirs);
  await page.locator('#submit').click();
  await expect(conflict).toBeVisible();
  await expect(page.locator('#submit')).toBeDisabled();
  await conflict.getByRole('button', { name: 'Server', exact: true }).click();
  await conflict.getByRole('button', { name: 'Apply resolution', exact: true }).click();
  await expect(page.locator('#submit')).toBeEnabled();
  await page.locator('#submit').click();
  await expect(page.getByRole('status').filter({ hasText: 'Changes committed successfully.' })).toBeVisible();
  const cache = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!)!));
  expect(cache.pages[path].text).toContain('Newer server passage.');
  expect(cache.pages[path].text).toContain('Local ending.');
});

test('Starlark apps render SVAR views and stage validated actions offline', async ({ page, context }) => {
  await page.goto('/#tasks%2Fv1%2Fapp.md');
  await expect(page.getByRole('button', { name: 'Tasks', exact: true })).toBeVisible();
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  await expect(page.locator('.apps-page')).toHaveCount(0);
  await page.getByRole('radio', { name: 'Render', exact: true }).click();

  await expect(page.getByRole('button', { name: 'Plan a project', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Schedule', exact: true }).click();
  await expect(page.getByLabel('Collection schedule')).toContainText('Plan a project');
  await expect(page.getByLabel('Collection schedule').locator('.wx-bar.resource')).toHaveCount(2);
  await expect(page.getByLabel('Collection schedule').locator('g[data-source]')).toHaveCount(1);
  await page.screenshot({ path: '/tmp/mdstore-app-gantt.png' });
  await page.getByRole('button', { name: 'Workload', exact: true }).click();
  await expect(page.getByRole('region', {name:'Collection schedule'})).toContainText('Remco');
  await expect(page.getByRole('region', {name:'Collection schedule'}).getByRole('button', {name:'Plan a project',exact:true})).toBeVisible();
  await page.getByRole('button', { name: 'Board', exact: true }).click();
  await expect(page.getByLabel('Move Plan a project')).toHaveValue('inbox');
  const card = await page.locator('[data-kanban-card-id]').first().boundingBox();
  const target = await page.locator('.wx-column').filter({ has: page.getByRole('heading', { name: 'ready', exact: true }) }).locator('[data-kanban-column-cards]').boundingBox();
  await page.mouse.move(card!.x + 30, card!.y + 10);
  await page.mouse.down();
  await page.mouse.move(target!.x + 40, target!.y + 50, { steps: 15 });
  await page.mouse.up();
  await expect(page.getByLabel('Move Plan a project')).toHaveValue('ready');
  await expect(page.getByRole('status').filter({ hasText: 'Changes staged.' })).toBeVisible();
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await page.getByLabel('Description').fill('Plan from the board');
  await expect(page.locator('#submit')).toBeEnabled();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText('Changed state to ready.');
  await page.getByRole('treeitem', { name: 'app.md', exact: true }).click();
  await page.getByRole('button', { name: 'Board', exact: true }).click();
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.getByLabel('Move Plan a project').selectOption('cancelled');
  await expect(page.getByLabel('Move Plan a project')).toHaveValue('cancelled');
  await expect(page.getByRole('status').filter({ hasText: 'Changes staged.' })).toBeVisible();
  await expect(page.locator('.apps-page')).toContainText('Offline');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Board', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Board', exact: true }).click();
  await expect(page.getByLabel('Move Plan a project')).toHaveValue('cancelled');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#navigation-sidebar').waitFor({ state: 'hidden' });
  await page.locator('.app-card-content').getByRole('button', { name: 'Plan a project', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/mdstore-app-board-mobile.png' });
  await page.locator('.app-card-content').getByRole('button', { name: 'Plan a project', exact: true }).click();
  await expect(page.locator('#preview')).toContainText('Plan a project');
});


test('computed schema errors are concise and link to their template source', async ({ page }) => {
  await page.goto('/#tasks%2Fv1%2F2026%2F09%2F23-001-plan.md');
  await expect(page.locator('#preview')).toContainText('Plan a project');
  await replaceDocument(page, '---\nstate: completed\ntags: [demo]\nstart: 2026-09-23\nend: 2026-09-25\nassignee: Remco\neffort: 3\n---\n# Plan a project\n\n## Timeline\n\n- 2026-09-23T08:00:00Z — Captured.\n');
  const note = page.locator('.validation-diagnostic').filter({hasText:'Transition inbox -> completed is not permitted'});
  await expect(note).toBeVisible();
  await expect(note).not.toContainText('Traceback');
  await expect(note).not.toContainText('[mdstore-field:');
  const link = note.getByRole('link');
  const label = await link.innerText();
  const line = Number(label.split(':').at(-1));
  expect(label).toMatch(/^tasks\/v1\/template.md:\d+$/);
  await link.click();
  await expect(page.getByRole('radio', {name:'Edit',exact:true})).toBeChecked();
  await expect(page).toHaveURL(/#tasks%2Fv1%2Ftemplate.md$/);
  await expect(page.locator(`.code-editor [data-line="${line}"]`).first()).toContainText('require(new in transitions[old]');
  await expect(page.locator(`.code-editor [data-line="${line}"]`).first()).toBeInViewport();
});


test('resource timeline stacks overlapping tasks on the same resource row', async ({ page }) => {
  await page.goto('/#tasks%2Fv1%2Fapp.md');
  await page.getByRole('button', {name:'Workload',exact:true}).click();
  const chart = page.getByRole('region', {name:'Collection schedule'});
  await expect(chart.locator('.wx-bar.resource')).toHaveCount(1);
  await expect(chart).toContainText('Remco');
  await expect(chart.locator('g[data-source]')).toHaveCount(1);
  const first = chart.getByRole('button', {name:'Plan a project',exact:true});
  const second = chart.getByRole('button', {name:'Second overlapping task',exact:true});
  await expect(first).toBeVisible();
  await expect(second).toBeVisible();
  await expect.poll(async () => {
    const a = await first.boundingBox(), b = await second.boundingBox();
    return !!a && !!b && (a.y + a.height <= b.y || b.y + b.height <= a.y);
  }).toBe(true);
  await page.setViewportSize({width:390,height:844});
  await page.locator('#navigation-sidebar').waitFor({state:'hidden'});
  await expect(chart.locator('.wx-bar.resource')).toBeVisible();
  await page.getByRole('button', {name:'Fit tasks',exact:true}).click();
  await expect(chart.locator('g[data-source] path').first()).toHaveAttribute('d', /^M[0-9.]+,[0-9.]+ H/);
  await page.screenshot({path:'/tmp/mdstore-resource-mobile.png'});
  await first.click();
  await expect(page.locator('#preview')).toContainText('Plan a project');
});

test('Gantt gestures stage moves and edge resizing, cancel safely, and work offline', async ({page, context}) => {
  await page.goto('/#tasks%2Fv1%2Fapp.md');
  await page.getByRole('button',{name:'Schedule',exact:true}).click();
  const chart = page.getByRole('region',{name:'Collection schedule'});
  const task = chart.getByRole('button',{name:'Plan a project',exact:true});
  const path = 'tasks/v1/2026/09/23-001-plan.md';
  const draft = () => page.evaluate(path => {
    const key = Object.keys(localStorage).find(k => k.startsWith('mdstore:workspace:'))!;
    return JSON.parse(localStorage.getItem(key)!).drafts[path]?.text || '';
  },path);
  async function ready() { await expect(task).toHaveAttribute('aria-disabled','false'); }
  async function gesture(handle: ReturnType<typeof chart.getByRole>, days: number, duration: number, cancel = false) {
    await ready();
    const bar = await task.boundingBox(), box = await handle.boundingBox();
    await page.mouse.move(box!.x+box!.width/2,box!.y+box!.height/2);
    await page.mouse.down();
    await page.mouse.move(box!.x+box!.width/2+days*bar!.width/duration,box!.y+box!.height/2,{steps:12});
    if (cancel) await handle.press('Escape');
    await page.mouse.up();
  }
  await gesture(task,1,3);
  await expect.poll(draft).toContain('start: 2026-09-24');
  await expect.poll(draft).toContain('end: 2026-09-26');
  await expect(page).toHaveURL(/app.md$/);
  await gesture(chart.getByRole('button',{name:'Resize end of Plan a project',exact:true}),1,3);
  await expect.poll(draft).toContain('end: 2026-09-27');
  await gesture(chart.getByRole('button',{name:'Resize start of Plan a project',exact:true}),1,4);
  await expect.poll(draft).toContain('start: 2026-09-25');
  const before = await draft();
  await gesture(task,-1,3,true);
  expect(await draft()).toBe(before);
  await page.getByRole('button',{name:'Workload',exact:true}).click();
  await ready();
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await context.setOffline(true);
  await task.press('ArrowRight');
  await expect.poll(draft).toContain('start: 2026-09-26');
  await expect.poll(draft).toContain('end: 2026-09-28');
  await page.reload();
  await page.getByRole('button',{name:'Workload',exact:true}).click();
  await ready();
  await expect(task.locator('..')).toHaveAttribute('title',/2026-09-26 – 2026-09-28/);
  await page.setViewportSize({width:390,height:844});
  await page.locator('#navigation-sidebar').waitFor({state:'hidden'});
  await page.getByRole('button',{name:'Fit tasks',exact:true}).click();
  await ready();
  const box = (await task.boundingBox())!;
  const touch = await context.newCDPSession(page);
  const x = box.x + box.width/2, y = box.y + box.height/2;
  await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
  await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x+box.width/3,y}]});
  await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await expect.poll(draft).toContain('start: 2026-09-27');
  await expect.poll(draft).toContain('end: 2026-09-29');
  await touch.detach();
  await page.setViewportSize({width:1440,height:1000});

  await page.getByRole('treeitem',{name:'Submit',exact:true}).click();
  await expect(page.getByRole('region',{name:'Staged diff'})).toContainText('2026-09-29');
});

test('Markdown frontmatter uses YAML highlighting without consuming later Markdown', async ({page, context}) => {
  await page.goto('/#other.md');
  await replaceDocument(page, '---\nmdstore: example\ncount: 3\n---\n\n# Planner\n\n---\n\nmdstore: ordinary prose\n\n```starlark\nvalue = True\n```\n');
  const key = page.locator('.code-editor [data-line="2"] span[style]').filter({hasText:'mdstore'}).first();
  const heading = page.locator('.code-editor [data-line="6"] span[style]').filter({hasText:'Planner'}).first();
  const starlark = page.locator('.code-editor [data-line="13"] span[style]').filter({hasText:'True'}).first();
  await expect(key).toBeVisible();
  await expect(heading).toBeVisible();
  await expect(starlark).toBeVisible();
  const proseColor = await page.locator('.code-editor [data-line="10"]').evaluate(el => getComputedStyle(el.querySelector('span') || el).color);
  await expect.poll(() => key.evaluate(el => getComputedStyle(el).color)).not.toBe(proseColor);
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  await context.setOffline(true);
  await page.reload();
  await page.getByRole('radio',{name:'Edit',exact:true}).click();
  await expect(key).toBeVisible();
  await expect.poll(() => key.evaluate(el => getComputedStyle(el).color)).not.toBe(proseColor);
  await expect(starlark).toBeVisible();
});

async function directoryPaths(page: Page, path: string): Promise<string[]> {
  const response = await page.request.post('/mcp', { data: {
    jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'get_page', arguments: { path } }
  } });
  const result = (await response.json()).result;
  expect(result.isError).toBe(false);
  return result.structuredContent.children.map((child: { path: string }) => child.path);
}

test('document selection changes remain incomplete locally but can be submitted', async ({ page }) => {
  test.skip(!process.env.MDSTORE_TEST_ADMIN, 'Requires privileged test daemon');
  await page.goto('/');
  await page.locator('[data-item-path="config.yaml"]').click();
  const source = await page.evaluate(() => {
    const key = Object.keys(localStorage).find(key => key.startsWith('mdstore:workspace:'))!;
    return JSON.parse(localStorage.getItem(key)!).pages['config.yaml'].text as string;
  });
  // Adding an unused exclusion changes selection without changing the current corpus.
  await replaceDocument(page, source.replace('documents:\n', 'documents:\n  exclude: ["unpublished/**"]\n'));
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('.validation-results')).toContainText('Local validation incomplete');
  await expect(page.locator('.validation-results')).not.toContainText('Validation passed');
  await page.getByLabel('Description').fill('Exclude unpublished documents');
  await expect(page.locator('#submit')).toBeEnabled();
  await page.locator('#submit').click();
  await expect(page.locator('.submit-notice')).toContainText('Changes committed successfully');
});
