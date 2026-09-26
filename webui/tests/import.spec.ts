import { test, expect } from './fixtures';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

test('folder import preserves hierarchy, stages Markdown, and uploads assets', async ({
  page,
  request
}) => {
  const root = mkdtempSync(join(tmpdir(), 'mdstore-import-'));
  const folder = join(root, 'bundle');
  mkdirSync(join(folder, 'notes'), { recursive: true });
  writeFileSync(
    join(folder, 'notes', 'readme.md'),
    '# Imported\n\nA new note.\n'
  );
  writeFileSync(join(folder, 'audio.wav'), 'audio fixture');
  writeFileSync(join(folder, 'ignored.exe'), 'unsupported');
  try {
    await page.goto('/webui/');
    await page.getByRole('treeitem', { name: 'Import', exact: true }).click();
    await page.getByLabel('Choose folder').setInputFiles(folder);
    await page.getByLabel('Destination folder').fill('incoming');
    const dialog = page.getByRole('dialog', { name: 'Import files' });
    await expect(
      dialog.getByText('incoming/bundle/notes/readme.md')
    ).toBeVisible();
    await dialog.getByRole('button', { name: 'Import', exact: true }).click();
    await expect(dialog.getByText('Staged', { exact: true })).toBeVisible();
    await expect(dialog.getByText('Uploaded', { exact: true })).toBeVisible();
    await expect(dialog.getByText('Skipped: unsupported file')).toBeVisible();
    const manifest = await (await request.get('/mcp/artifacts')).json();
    expect(manifest.assets['incoming/bundle/audio.wav']).toBeTruthy();
    expect(manifest.assets['incoming/bundle/notes/readme.md']).toBeUndefined();
    await dialog.getByRole('button', { name: 'Close' }).click();
    await page
      .getByRole('treeitem', { name: 'readme.md', exact: true })
      .click();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Imported', exact: true })
    ).toBeVisible();
    await expect(page.getByText('Modified', { exact: true })).toBeVisible();
    await expect(page.getByText('Valid', { exact: true })).toBeVisible();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('external file drops never overwrite existing documents or partially import a conflicting batch', async ({
  page,
  request
}) => {
  await page.goto('/webui/#welcome.md');
  await expect(
    page.getByRole('heading', { name: 'Welcome', exact: true })
  ).toBeVisible();
  await page.evaluate(() => {
    const data = new DataTransfer();
    data.items.add(new File(['# Overwritten\n'], 'welcome.md'));
    data.items.add(new File(['audio'], 'unexpected.wav'));
    document.body.dispatchEvent(
      new DragEvent('drop', { bubbles: true, dataTransfer: data })
    );
  });
  const dialog = page.getByRole('dialog', { name: 'Import files' });
  await expect(dialog.getByText('welcome.md', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Import', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText(
    'Destination already exists'
  );
  expect(
    (await (await request.get('/mcp/artifacts')).json()).assets[
      'unexpected.wav'
    ]
  ).toBeUndefined();
});
