import { describe, expect, it } from 'vitest';
import { emptyCache } from '../workspace/cache';
import { addFolder, moveTree, treePaths } from './treeEdits';
import { rewriteLinks } from './rewriteLinks';
import { editRequest } from '../workspace/api';

function fixture() {
  const cache = emptyCache('test');
  cache.paths = ['a.md', 'notes/b.md'];
  cache.pages = Object.fromEntries(
    cache.paths.map((path) => [
      path,
      {
        path,
        exists: true,
        text: path === 'a.md' ? '[B](notes/b.md#part)\n' : '[A](../a.md)\n',
        template: null
      }
    ])
  );
  cache.selected = 'notes/b.md';
  return cache;
}
describe('staged tree edits', () => {
  it('moves dirty documents and updates incoming and outgoing links atomically', () => {
    const cache = fixture();
    cache.drafts['notes/b.md'] = {
      ...cache.pages['notes/b.md'],
      base: cache.pages['notes/b.md'].text,
      text: '[A](../a.md)\nLocal edit\n'
    };
    const next = moveTree(cache, 'notes', 'archive/deep', true, {});
    expect(next.selected).toBe('archive/deep/b.md');
    expect(next.drafts['a.md'].text).toBe('[B](archive/deep/b.md#part)\n');
    expect(next.drafts['archive/deep/b.md'].text).toBe(
      '[A](../../a.md)\nLocal edit\n'
    );
    expect(next.deletions).toEqual({ 'notes/b.md': '[A](../a.md)\n' });
    expect(editRequest('', next.drafts, next.deletions).edits).toHaveLength(3);
    expect(cache.drafts['notes/b.md'].text).toContain('Local edit');
    const back = moveTree(next, 'archive/deep', 'notes', true, {});
    expect(back.deletions).toEqual({});
    expect(back.drafts['a.md']).toBeUndefined();
    expect(back.drafts['notes/b.md'].base).toBe('[A](../a.md)\n');
  });
  it('preserves empty folders and rejects collisions and missing offline sources', () => {
    const cache = addFolder(fixture(), 'empty');
    expect(treePaths(moveTree(cache, 'empty', 'renamed', true, {}))).toContain(
      'renamed/'
    );
    expect(() => moveTree(cache, 'a.md', 'notes', false, {})).toThrow('exists');
    delete cache.pages['a.md'];
    expect(() => moveTree(cache, 'notes', 'other', true, {})).toThrow(
      'Cache a.md'
    );
    expect(cache.deletions).toBeUndefined();
  });
});
it('rewrites parsed Markdown links and reference definitions, preserving code and titles', () => {
  const input =
    '[x](a.md "title") ![image](a.md) [r][ref]\n\n[ref]: <a.md#h> "title"\n`[code](a.md)`\n```md\n[x](a.md)\n```\nNot a link ](a.md)\n[x](\n a.md\n)\n';
  const result = rewriteLinks(
    input,
    'index.md',
    'index.md',
    new Map([['a.md', 'new/a.md']])
  );
  expect(result).toBe(
    '[x](new/a.md "title") ![image](new/a.md) [r][ref]\n\n[ref]: <new/a.md#h> "title"\n`[code](a.md)`\n```md\n[x](a.md)\n```\nNot a link ](a.md)\n[x](\n new/a.md\n)\n'
  );
});

it('updates extensionless and unique short links when their target is renamed', () => {
  expect(
    rewriteLinks(
      '[b](b) [b](b.md)',
      'a.md',
      'a.md',
      new Map([['notes/b.md', 'new/c.md']]),
      ['a.md', 'notes/b.md']
    )
  ).toBe('[b](new/c.md) [b](new/c.md)');
});

it('deletes whole folders atomically using original bases, including local-only drafts', async () => {
  const { deleteTree } = await import('./treeEdits');
  const cache = addFolder(fixture(), 'notes/empty');
  cache.drafts['notes/b.md'] = {
    ...cache.pages['notes/b.md'],
    base: cache.pages['notes/b.md'].text,
    text: 'Unsaved edit\n'
  };
  cache.drafts['notes/new.md'] = {
    path: 'notes/new.md',
    exists: false,
    base: '',
    text: 'New\n',
    template: null
  };
  const next = deleteTree(cache, 'notes', true, {});
  expect(next.deletions).toEqual({ 'notes/b.md': '[A](../a.md)\n' });
  expect(next.drafts).toEqual({});
  expect(next.folders).toEqual([]);
  expect(next.selected).toBe('');
  expect(treePaths(next)).toEqual(['a.md']);
  expect(cache.drafts['notes/b.md'].text).toBe('Unsaved edit\n');
  cache.paths.push('notes/template.md');
  expect(() => deleteTree(cache, 'notes', true, {})).toThrow('not permitted');
  expect(cache.deletions).toBeUndefined();
});
