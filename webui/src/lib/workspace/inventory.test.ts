import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { webcrypto } from 'node:crypto';
import { type Directory, type Page } from './api';
import { readInventory } from './inventory';

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());
const file = (path: string, text = path): Page => ({
  path,
  text,
  exists: true,
  template: null
});
const directory = (
  path: string,
  children: Directory['children']
): Directory => ({ path, children });
const entry = (path: string): Directory['children'][number] => ({
  path,
  kind: 'file'
});

describe('MCP document inventory', () => {
  it('traverses plain directory listings and reads every source', async () => {
    const api = {
      directory: vi.fn(async (path = '/') =>
        path === '/'
          ? directory('/', [
              entry('a.md'),
              { path: 'notes/', kind: 'directory' }
            ])
          : directory(path, [entry('notes/b.md')])
      ),
      page: vi.fn(async (path: string) => file(path))
    };
    const result = await readInventory(api);
    expect(result.paths).toEqual(['a.md', 'notes/b.md']);
    expect(api.page.mock.calls).toEqual([['a.md'], ['notes/b.md']]);
    expect(result.revision).toMatch(/^[a-f0-9]{64}$/);
  });
  it('detects changed content even when the path list is unchanged', async () => {
    let text = 'original';
    const api = {
      directory: vi.fn(async () => directory('/', [entry('a.md')])),
      page: vi.fn(async (path: string) => file(path, text))
    };
    const first = await readInventory(api);
    text = 'changed';
    const second = await readInventory(api);
    expect(second.revision).not.toBe(first.revision);
    expect(second.pages['a.md'].text).toBe('changed');
  });
  it('retries if a directory disappears during traversal', async () => {
    let removed = false;
    const api = {
      directory: vi.fn(async (path = '/') => {
        if (path !== '/') {
          removed = true;
          throw Error('directory not found');
        }
        return directory(
          '/',
          removed ? [] : [{ path: 'old/', kind: 'directory' }]
        );
      }),
      page: vi.fn()
    };
    expect((await readInventory(api)).paths).toEqual([]);
  });
  it('retries when paths change during a read', async () => {
    let path = 'a.md';
    const api = {
      directory: vi.fn(async () => directory('/', [entry(path)])),
      page: vi.fn(async (requested: string) => {
        path = 'b.md';
        return file(requested);
      })
    };
    expect((await readInventory(api)).paths).toEqual(['b.md']);
    expect(api.page).toHaveBeenCalledTimes(2);
  });
  it('fails if listed files keep disappearing', async () => {
    const api = {
      directory: vi.fn(async () => directory('/', [entry('a.md')])),
      page: vi.fn(async (path: string) => ({ ...file(path), exists: false }))
    };
    await expect(readInventory(api)).rejects.toThrow('Document tree changed');
    expect(api.page).toHaveBeenCalledTimes(3);
  });
});
