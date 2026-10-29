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

test('drafts are isolated per tab, survive refresh, and search requires a connection', async ({
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
  await replaceDocument(page, '# Welcome\n\nSaved local draft.\n');
  await expect(page.locator('.document-header')).toContainText('Valid');
  await page.reload();
  await expect(page.locator('#preview')).toContainText('Saved local draft.');
  const secondTab = await context.newPage();
  await secondTab.goto('/#welcome.md');
  await expect(secondTab.locator('#preview')).toContainText(
    'A knowledge garden for your ideas.'
  );
  await replaceDocument(secondTab, '# Welcome\n\nSecond tab draft.\n');
  await secondTab.reload();
  await expect(secondTab.locator('#preview')).toContainText(
    'Second tab draft.'
  );
  await page.reload();
  await expect(page.locator('#preview')).toContainText('Saved local draft.');
  await secondTab.close();
  const freshTab = await context.newPage();
  await freshTab.goto('/#welcome.md');
  await expect(freshTab.locator('#preview')).toContainText(
    'A knowledge garden for your ideas.'
  );
  await freshTab.close();

  expect(
    await page.evaluate(() =>
      navigator.serviceWorker.getRegistrations().then((items) => items.length)
    )
  ).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  await page.getByRole('treeitem', { name: 'Search', exact: true }).click();
  await page.locator('#search').fill('garden');
  await page.locator('#search').press('Enter');
  await expect(page.getByText(/MCP RESULTS/)).toBeVisible();
  expect(calls).toContain('search');
  expect(calls).toContain('GET /path');
  await context.setOffline(true);
  await expect(page.locator('#search')).toBeDisabled();
  await expect(
    page.getByText('Connect to the daemon to search your documents.')
  ).toBeVisible();
  await context.setOffline(false);
  await page.getByRole('treeitem', { name: 'Settings', exact: true }).click();
  await expect(
    page.getByRole('status').filter({ hasText: /^Connected$/ })
  ).toBeVisible();
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Discard local changes' }).click();
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
  const journal = await page.evaluate(() =>
    JSON.parse(
      sessionStorage.getItem(
        Object.keys(sessionStorage).find((key) =>
          key.startsWith('mdstore:workspace:')
        )!
      )!
    )
  );
  expect(journal.drafts).toEqual({});
  expect(journal.deletions).toEqual({});
  expect(journal.pages).toBeUndefined();
  expect(journal.paths).toBeUndefined();
  expect(journal.validationSnapshot).toBeUndefined();
  await page.reload();
  await page.getByRole('treeitem', { name: 'other.md', exact: true }).click();
  await expect(page.locator('#preview')).toContainText(
    'Reviewed in the submit pane.'
  );
  expect(journal.summary).toBe('');
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
