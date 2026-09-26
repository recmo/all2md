import { test, expect } from './fixtures';

test.skip(
  !process.env.MDSTORE_TEST_ADMIN,
  'Requires configuration write access'
);

test('recording review stages guidance and derived transcripts remain read-only', async ({
  page,
  request
}) => {
  const text =
    '---\nmdstore: recording\naudio: audio.wav\nhotwords: []\nattendees: []\n---\n# Recording\n\nHuman notes.\n';
  const created = await request.post('/mcp', {
    data: {
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: {
        name: 'edit',
        arguments: {
          edit_summary: 'Add recording',
          edits: [{ op: 'create_page', path: 'recording.md', content: text }]
        }
      }
    }
  });
  expect((await created.json()).result.isError).toBeFalsy();
  const audio = Buffer.alloc(44 + 16000 * 2 * 3);
  audio.write('RIFF', 0);
  audio.writeUInt32LE(audio.length - 8, 4);
  audio.write('WAVEfmt ', 8);
  audio.writeUInt32LE(16, 16);
  audio.writeUInt16LE(1, 20);
  audio.writeUInt16LE(1, 22);
  audio.writeUInt32LE(16000, 24);
  audio.writeUInt32LE(32000, 28);
  audio.writeUInt16LE(2, 32);
  audio.writeUInt16LE(16, 34);
  audio.write('data', 36);
  audio.writeUInt32LE(audio.length - 44, 40);
  const upload = await request.put('/audio.wav', {
    data: audio,
    headers: { 'If-None-Match': '*' }
  });
  expect(upload.ok()).toBeTruthy();
  await page.goto('/webui/#recording.md');
  await page.getByRole('button', { name: 'Enable transcription' }).click();
  await expect(page.getByText('queued', { exact: true })).toBeVisible();
  const headers = { Authorization: 'Bearer browser-test-worker' };
  const assignment = await (
    await request.post('http://127.0.0.1:43133/worker', {
      headers,
      data: { op: 'claim', recipes: ['speech2md-v1'] }
    })
  ).json();
  const artifact = await (
    await request.put(
      `http://127.0.0.1:43133/transcript.voiceprints.json?job=${assignment.job.id}&attempt=${assignment.job.attempt}`,
      {
        headers,
        data: JSON.stringify({
          space: { namespace: 'speakers', recipe: 'fixture', dimensions: 2 },
          records: []
        })
      }
    )
  ).json();
  const completed = await request.post('http://127.0.0.1:43133/worker', {
    headers,
    data: {
      op: 'complete',
      id: assignment.job.id,
      attempt: assignment.job.attempt,
      outputs: {
        'transcript.md':
          '# Transcript\n\n**[00:00:01.00] speaker-1:** Hello world. <!-- 2.00s -->\n'
      },
      assets: { 'transcript.voiceprints.json': artifact }
    }
  });
  expect(completed.ok()).toBeTruthy();
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('button', { name: 'Regenerate', exact: true }).click();
  await expect(page.getByText('queued', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /Hello world/ }).click();
  await page.getByLabel('Speaker', { exact: true }).fill('Alice');
  await page.getByRole('button', { name: 'Stage assignment' }).click();
  await expect(
    page.getByText('Review changes are staged locally.')
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: '/tmp/mdstore-speech-review.png',
    fullPage: true
  });
  await page.getByRole('button', { name: 'Open transcript' }).click();
  await expect(page.getByText('Read only', { exact: true })).toBeVisible();
  await page.getByText('Edit', { exact: true }).click();
  await expect(page.locator('[contenteditable="true"]')).toHaveCount(0);
});
