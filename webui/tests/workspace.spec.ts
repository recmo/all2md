import { type Page } from '@playwright/test';
import { test, expect } from './fixtures';

test('browser history visits documents and workspace views without duplicate entries', async ({
  page
}) => {
  await page.goto('/');
  await expect(
    page.getByRole('treeitem', { name: 'welcome.md', exact: true })
  ).toBeVisible();
  const initialLength = await page.evaluate(() => history.length);
  await page.getByRole('treeitem', { name: 'welcome.md', exact: true }).click();
  await expect(page.locator('#preview')).toContainText('Welcome');
  await page
    .locator('#preview')
    .getByRole('link', { name: 'Other note' })
    .click();
  await expect(page.locator('#preview')).toContainText('Keep it simple.');
  for (const name of ['Search', 'Settings', 'Submit']) {
    await page.getByRole('treeitem', { name, exact: true }).click();
    await expect(
      page.getByRole('heading', { name, exact: true })
    ).toBeVisible();
  }
  expect(await page.evaluate(() => history.length)).toBe(initialLength + 5);
  for (const name of ['Settings', 'Search']) {
    await page.goBack();
    await expect(
      page.getByRole('heading', { name, exact: true })
    ).toBeVisible();
  }
  await page.goBack();
  await expect(page.locator('.document-breadcrumb')).toHaveText('other.md');
  await page.goBack();
  await expect(page.locator('.document-breadcrumb')).toHaveText('welcome.md');
  await page.goBack();
  await expect(page.locator('.empty')).toContainText(
    'Your knowledge, connected.'
  );
  for (const path of ['welcome.md', 'other.md']) {
    await page.goForward();
    await expect(page.locator('.document-breadcrumb')).toHaveText(path);
  }
  await page.goForward();
  await expect(
    page.getByRole('heading', { name: 'Search', exact: true })
  ).toBeVisible();
  expect(await page.evaluate(() => history.length)).toBe(initialLength + 5);
});

test('offline editing survives reload and search uses the MCP API online', async ({
  page,
  context
}) => {
  const calls: string[] = [];
  page.on('request', (r) => {
    if (r.url().endsWith('/mcp')) calls.push(r.postDataJSON().params.name);
    if (new URL(r.url()).pathname.startsWith('/path/')) calls.push('GET /path');
  });
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'welcome.md', exact: true }).click();
  await replaceDocument(page, '# Welcome\n\nOffline knowledge garden.\n');
  await expect(page.locator('.document-header')).toContainText('Valid');
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null);
  // A stale installed shell must not hide the current HTML on refresh.
  await page.evaluate(async () => {
    const key = (await caches.keys()).find((key) =>
      key.startsWith('mdstore-shell-')
    )!;
    const cache = await caches.open(key);
    const shell = (await cache.match('/'))!;
    await cache.put(
      '/',
      new Response(
        (await shell.text()).replace(
          '<head>',
          '<head><meta name="cached-shell" content="stale">'
        ),
        { headers: shell.headers }
      )
    );
  });
  await page.reload();
  await expect(page.locator('meta[name="cached-shell"]')).toHaveCount(0);
  await expect(page.locator('#preview')).toContainText(
    'Offline knowledge garden.'
  );
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator('meta[name="cached-shell"]')).toHaveCount(1);
  await expect(page.locator('#preview')).toContainText(
    'Offline knowledge garden.'
  );
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').fill('garden');
  await page.locator('#search').press('Enter');
  await expect(page.getByText('OFFLINE · CACHED TEXT')).toBeVisible();
  await context.setOffline(false);
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await expect(
    page.getByRole('status').filter({ hasText: /^Connected$/ })
  ).toBeVisible();
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').press('Enter');
  await expect(page.getByText(/MCP RESULTS/)).toBeVisible();
  expect(calls).toContain('search');
  expect(calls).toContain('GET /path');
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await page.getByText('Local storage ·', { exact: false }).click();
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Clear local cache' }).click();
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(
    page.getByRole('status').filter({ hasText: /^Connected$/ })
  ).toBeVisible();
  await page.getByRole('treeitem', { name: 'welcome.md', exact: true }).click();
  await expect(page.locator('#preview')).toContainText(
    'A knowledge garden for your ideas.'
  );
});

test('submit pane reviews validation and diffs, then commits the full staged batch', async ({
  page,
  context
}) => {
  await page.goto('/');
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await replaceDocument(
    page,
    '# Other note\n\n[Missing](missing-submit-target.md)\n'
  );
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Submit', exact: true })
  ).toBeVisible();
  await page.getByLabel('Description').fill('Review and reorganize notes');
  await expect(
    page.getByRole('list', { name: 'Validation errors' })
  ).toContainText('missing-submit-target');
  await expect(page.locator('#submit')).toBeDisabled();
  await page
    .getByRole('list', { name: 'Validation errors' })
    .getByRole('button')
    .first()
    .click();
  await replaceDocument(page, '# Other note\n\nReviewed in the submit pane.\n');
  await page
    .getByRole('treeitem', { name: 'welcome.md', exact: true })
    .dblclick();
  await page
    .getByRole('textbox', { name: 'Rename welcome.md', exact: true })
    .fill('welcome-submitted.md');
  await page
    .getByRole('textbox', { name: 'Rename welcome.md', exact: true })
    .press('Enter');
  await expect(
    page.getByRole('treeitem', { name: 'welcome-submitted.md', exact: true })
  ).toBeVisible();
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await expect(page.locator('#submit')).toBeEnabled();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText(
    'Added'
  );
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText(
    'Deleted'
  );
  await expect(page.locator('.staged-diff').first()).toBeVisible();
  await expect(
    page
      .locator('.staged-file')
      .filter({ hasText: 'other.md' })
      .locator('[data-line]')
      .filter({ hasText: 'Reviewed in the submit pane.' })
  ).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Description')).toHaveValue(
    'Review and reorganize notes'
  );
  await context.setOffline(true);
  await expect(page.locator('#submit')).toBeDisabled();
  await context.setOffline(false);
  await expect(page.locator('#submit')).toBeEnabled();
  await page.screenshot({ path: '/tmp/mdstore-submit-pane.png' });
  const applied = page.waitForRequest(
    (request) =>
      request.url().endsWith('/mcp') &&
      request.postDataJSON()?.params?.name === 'edit'
  );
  await page.locator('#submit').click();
  expect(
    Object.keys((await applied).postDataJSON().params.arguments.edits)
  ).toHaveLength(3);
  await expect(
    page
      .getByRole('status')
      .filter({ hasText: 'Changes committed successfully.' })
  ).toBeVisible();
  await expect(page.getByRole('region', { name: 'Staged diff' })).toContainText(
    'no staged changes'
  );
  const listing = await directoryPaths(page, '/');
  expect(listing).toContain('welcome-submitted.md');
  expect(listing).not.toContain('welcome.md');
  const cache = await page.evaluate(() =>
    JSON.parse(
      localStorage.getItem(
        Object.keys(localStorage).find((key) =>
          key.startsWith('mdstore:workspace:')
        )!
      )!
    )
  );
  expect(cache.drafts).toEqual({});
  expect(cache.deletions).toEqual({});
  expect(cache.pages).toEqual({});
  await expect
    .poll(() => cachedDocument(page, cache.repository, 'other.md'))
    .toContain('Reviewed in the submit pane.');
  expect(cache.summary).toBe('');
});

async function replaceDocument(page: Page, text: string) {
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  const editor = page.locator('.code-editor [role=textbox]');
  await editor.focus();
  await editor.press('ControlOrMeta+a');
  await page.keyboard.insertText(text);
}

async function directoryPaths(page: Page, path: string): Promise<string[]> {
  const response = await page.request.get('/path/' + path.replace(/^\//, ''));
  expect(response.ok()).toBe(true);
  expect(response.headers()['x-mdstore-commit']).toMatch(/^[0-9a-f]{40,64}$/);
  const text = await response.text();
  return text
    .split('\n')
    .filter(Boolean)
    .map((path: string) => path.replace(/^\//, ''));
}

async function cachedDocument(page: Page, repository: string, path: string) {
  return page.evaluate(
    ({ repository, path }) =>
      new Promise<string | undefined>((resolve, reject) => {
        const open = indexedDB.open('mdstore-documents', 1);
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const db = open.result;
          const request = db
            .transaction('documents', 'readonly')
            .objectStore('documents')
            .get([repository, path]);
          request.onsuccess = () => {
            resolve(request.result?.text);
            db.close();
          };
          request.onerror = () => {
            reject(request.error);
            db.close();
          };
        };
      }),
    { repository, path }
  );
}
