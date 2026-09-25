import { describe, expect, it, vi } from 'vitest';
import { type Api, type Directory, type Page } from './api';
import { readInventory } from './inventory';

const file = (path: string, revision = 'a', hash = path): Page => ({
  path,
  revision,
  hash,
  exists: true,
  text: path,
  template: null
});
const directory = (
  path: string,
  revision: string,
  children: Directory['children']
): Directory => ({
  path,
  revision,
  children,
  kind: 'directory',
  repository: 'repo',
  allow_template_edits: false,
  allow_config_edits: false
});
const entry = (path: string, hash = path): Directory['children'][number] => ({
  path,
  hash,
  kind: 'file'
});

describe('MCP document inventory', () => {
  it('traverses directories and reuses unchanged sources across revisions', async () => {
    const api = {
      directory: vi.fn(async (path = '/') =>
        path === '/'
          ? directory('/', 'b', [
              entry('a.md'),
              { path: 'notes/', kind: 'directory' }
            ])
          : directory(path, 'b', [entry('notes/b.md')])
      ),
      page: vi.fn(async (path: string) => file(path, 'b'))
    };
    const result = await readInventory(api as unknown as Api, {
      'a.md': file('a.md')
    });
    expect(result.paths).toEqual(['a.md', 'notes/b.md']);
    expect(api.page.mock.calls).toEqual([['notes/b.md']]);
    expect(result.root.revision).toBe('b');
  });

  it('retries the entire read when a file has a different revision', async () => {
    let revision = 'a';
    const api = {
      directory: vi.fn(async () => directory('/', revision, [entry('a.md')])),
      page: vi.fn(async (path: string) => {
        revision = 'b';
        return file(path, revision);
      })
    };
    const result = await readInventory(api as unknown as Api, {});
    expect(result.root.revision).toBe('b');
    expect(api.page).toHaveBeenCalledTimes(2);
  });

  it('retries if a directory disappears during traversal', async () => {
    let revision = 'a';
    const api = {
      directory: vi.fn(async (path = '/') => {
        if (path !== '/') {
          revision = 'b';
          throw Error('directory not found');
        }
        return directory(
          '/',
          revision,
          revision === 'a' ? [{ path: 'old/', kind: 'directory' }] : []
        );
      }),
      page: vi.fn()
    };
    expect((await readInventory(api as unknown as Api, {})).paths).toEqual([]);
  });

  it('refreshes template projections when policy source changes', async () => {
    const api = {
      directory: vi.fn(async () =>
        directory('/', 'b', [entry('a.md'), entry('template.md', 'new')])
      ),
      page: vi.fn(async (path: string) =>
        file(path, 'b', path === 'template.md' ? 'new' : path)
      )
    };
    await readInventory(api as unknown as Api, {
      'a.md': file('a.md'),
      'template.md': file('template.md')
    });
    expect(api.page.mock.calls.map(([path]) => path).sort()).toEqual([
      'a.md',
      'template.md'
    ]);
  });

  it('fails rather than returning an inventory assembled across revisions', async () => {
    const api = {
      directory: vi.fn(async () => directory('/', 'a', [entry('a.md')])),
      page: vi.fn(async (path: string) => file(path, 'b'))
    };
    await expect(readInventory(api as unknown as Api, {})).rejects.toThrow(
      'Repository changed'
    );
    expect(api.page).toHaveBeenCalledTimes(3);
  });
});
