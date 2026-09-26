import { test, expect } from './fixtures';

test('repository events refresh a clean document without polling or reconnecting', async ({
  page
}) => {
  await page.goto('/webui/#other.md');
  await expect(page.locator('#preview')).toContainText('Keep it simple.');
  const original = await page.request.get('/other.md');
  const updated = await page.request.put('/other.md', {
    headers: { 'If-Match': original.headers().etag },
    data: '# Other note\n\nChanged in another client.\n'
  });
  expect(updated.ok()).toBe(true);
  await expect(page.locator('#preview')).toContainText(
    'Changed in another client.'
  );
});

test('a committed submission with a lost response is retried without duplicating it', async ({
  page
}) => {
  await page.goto('/webui/#other.md');
  await page.getByRole('radio', { name: 'Edit', exact: true }).click();
  const editor = page.locator('.code-editor [role=textbox]');
  await editor.focus();
  await editor.press('ControlOrMeta+a');
  await page.keyboard.insertText('# Other note\n\nSurvives a lost response.\n');
  await page.getByRole('treeitem', { name: 'Submit', exact: true }).click();
  await page.getByLabel('Description').fill('Retry a committed batch');
  await expect(page.locator('#submit')).toBeEnabled();
  const bodies: string[] = [];
  await page.route('**/mcp', async (route) => {
    if (route.request().postDataJSON()?.params?.name !== 'apply_edits')
      return route.continue();
    bodies.push(route.request().postData()!);
    if (bodies.length === 1) {
      const committed = await route.fetch();
      expect((await committed.json()).result.isError).toBe(false);
      await route.abort('connectionclosed');
    } else await route.continue();
  });
  await page.locator('#submit').click();
  await expect(page.locator('.submit-notice')).toContainText(
    'already submitted'
  );
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toBe(bodies[1]);
});
