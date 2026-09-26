import { test, expect } from './fixtures';

test.use({ apiToken: 'browser-secret' });
test('cookie login authorizes file reads and writes, survives reload, and revokes on logout', async ({
  page,
  context
}) => {
  await page.goto('/webui/settings');
  await page.getByLabel('Bearer token').fill('browser-secret');
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(
    page.getByRole('status').filter({ hasText: 'Connected.' })
  ).toBeVisible();
  const cookie = (await context.cookies()).find(
    (c) => c.name === 'mdstore_session'
  );
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe('Strict');
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    'mdstore_session'
  );
  await page.reload();
  const statuses = await page.evaluate(async () => {
    const read = await fetch('/other.md');
    const create = await fetch('/cookie.wav', {
      method: 'PUT',
      headers: { 'If-None-Match': '*' },
      body: 'audio'
    });
    const media = await fetch('/cookie.wav', {
      headers: { Range: 'bytes=1-2' }
    });
    return [read.status, create.status, media.status, await media.text()];
  });
  expect(statuses).toEqual([200, 201, 206, 'ud']);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(
    page.getByRole('status').filter({ hasText: 'Signed out.' })
  ).toBeVisible();
  expect(
    await page.evaluate(async () => (await fetch('/other.md')).status)
  ).toBe(401);
});
