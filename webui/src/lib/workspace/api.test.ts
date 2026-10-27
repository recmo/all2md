import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';

afterEach(() => vi.unstubAllGlobals());

it('requests exact source once and expanded schema for the editor', async () => {
  const source = '---\r\nname: Alice\r\n---\r\n# Notes\r\n';
  const template = {
    path: 'schema.md',
    content: 'Schema instructions',
    definition: { frontmatter: { type: 'object' } }
  };
  const fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      result: {
        isError: false,
        structuredContent: {
          path: 'alice.md',
          exists: true,
          content: source,
          template
        }
      }
    })
  });
  vi.stubGlobal('fetch', fetch);
  const page = await api.page('alice.md');
  expect(JSON.parse(fetch.mock.calls[0][1].body).params).toEqual({
    name: 'get_page',
    arguments: { path: 'alice.md', format: 'raw', include_template: true }
  });
  expect(page.text).toBe(source);
  expect(page.template).toEqual(template);
  expect(page).not.toHaveProperty('content');
});
