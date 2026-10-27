import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';

afterEach(() => vi.unstubAllGlobals());

it('requests exact source once and receives only the schema path', async () => {
  const source = '---\r\nname: Alice\r\n---\r\n# Notes\r\n';
  const template = '/schema.md';
  const fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      result: {
        isError: false,
        structuredContent: {
          path: '/alice.md',
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
    arguments: { path: 'alice.md', format: 'raw' }
  });
  expect(page.text).toBe(source);
  expect(page.exists).toBe(true);
  expect(page.path).toBe('alice.md');
  expect(page.template).toEqual({ path: 'schema.md' });
  expect(page).not.toHaveProperty('content');
});

it('distinguishes missing files from empty files without an exists field', async () => {
  for (const content of [null, '']) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          result: {
            isError: false,
            structuredContent: {
              path: 'new.md',
              template: null,
              content
            }
          }
        })
      })
    );
    const page = await api.page('new.md');
    expect(page.exists).toBe(content !== null);
    expect(page.text).toBe('');
  }
});

it('normalizes repository-rooted directory paths for the editor', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        result: {
          isError: false,
          content: [{ type: 'text', text: '/notes/a.md\n/notes/nested/' }]
        }
      })
    })
  );
  const directory = await api.directory('notes/');
  expect(directory.path).toBe('notes/');
  expect(directory.children.map((child) => child.path)).toEqual([
    'notes/a.md',
    'notes/nested/'
  ]);
});
