import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';

afterEach(() => vi.unstubAllGlobals());

it('requests exact source once and receives only the schema path', async () => {
  const source = '---\r\nname: Alice\r\n---\r\n# Notes\r\n';
  const fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: new Headers({ Link: '</path/schema.md>; rel="describedby"' }),
    text: async () => source
  });
  vi.stubGlobal('fetch', fetch);
  const page = await api.page('alice.md');
  expect(fetch.mock.calls[0][0]).toBe('/path/alice.md');
  expect(fetch.mock.calls[0][1].cache).toBe('no-store');
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
        ok: content !== null,
        status: content === null ? 404 : 200,
        headers: new Headers(),
        text: async () => content ?? ''
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
      status: 200,
      text: async () => '/notes/a.md\n/notes/nested/'
    })
  );
  const directory = await api.directory('notes/');
  expect(directory.path).toBe('notes/');
  expect(directory.children.map((child) => child.path)).toEqual([
    'notes/a.md',
    'notes/nested/'
  ]);
});
