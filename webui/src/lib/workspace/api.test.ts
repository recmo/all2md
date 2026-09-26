import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
afterEach(() => vi.unstubAllGlobals());
test('a lost submission response retries the exact original request', async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError('connection lost'))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          result: {
            isError: false,
            structuredContent: { status: 'already_applied' }
          }
        })
      )
    );
  vi.stubGlobal('fetch', fetch);
  const result = await new Api().apply({
    edit_summary: 'Change',
    edits: [{ op: 'create_page', path: 'a.md', content: '# A\n' }]
  });
  expect(result.status).toBe('already_applied');
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
});
test('HTTP and MCP validation errors preserve the same problem details', async () => {
  const problem = {
    type: 'urn:mdstore:problem:validation',
    title: 'Unprocessable Entity',
    status: 422,
    detail: 'Validation failed',
    findings: [{ path: 'a.md', line: 2, message: 'Broken link' }]
  };
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(problem), { status: 422 })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            result: {
              isError: true,
              content: [{ text: 'Validation failed' }],
              structuredContent: problem
            }
          })
        )
      )
  );
  const api = new Api();
  await expect(api.request('/a.md', { method: 'PUT' })).rejects.toMatchObject({
    message: problem.detail,
    status: 422,
    findings: problem.findings
  });
  await expect(
    api.apply({ edit_summary: 'Invalid', edits: [] })
  ).rejects.toMatchObject({
    message: problem.detail,
    status: 422,
    findings: problem.findings
  });
});

test('a truncated commit response also retries the original request', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response('{"result":'))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          result: {
            isError: false,
            structuredContent: { status: 'already_applied' }
          }
        })
      )
    );
  vi.stubGlobal('fetch', fetch);
  await expect(
    new Api().apply({ edit_summary: 'Change', edits: [] })
  ).resolves.toMatchObject({ status: 'already_applied' });
  expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
});
