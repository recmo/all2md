import { beforeEach, it, expect } from 'vitest';
import { emptyCache, saveCache, loadCache, cachedSearch } from './cache';
import { editRequest } from './api';
beforeEach(() => localStorage.clear());
it('restores exact draft bases and commit summaries, isolated per repository', () => {
  const cache = emptyCache('repo-a');
  cache.summary = 'My offline changes';
  cache.pages['a.md'] = {
    path: 'a.md',
    exists: true,
    text: '# A\r\n',
    template: null
  };
  cache.drafts['a.md'] = {
    ...cache.pages['a.md'],
    base: '# A\r\n',
    text: '# Updated\r\n'
  };
  saveCache(localStorage, cache);
  expect(loadCache(localStorage)).toEqual(cache);
  expect(loadCache(localStorage, 'repo-b').drafts).toEqual({});
  expect(editRequest(cache.summary, cache.drafts).edits).toEqual([
    {
      op: 'replace_page',
      path: 'a.md',
      base: '# A\r\n',
      content: '# Updated\r\n'
    }
  ]);
});
it('offline search is limited to cached text and is explicitly identified', () => {
  const cache = emptyCache('repo');
  cache.paths = ['cached.md', 'unread.md'];
  cache.pages['cached.md'] = {
    path: 'cached.md',
    text: 'Knowledge garden',
    exists: true,
    template: null
  };
  expect(cachedSearch(cache, 'garden')[0].matched_arms).toEqual([
    'offline text'
  ]);
  expect(cachedSearch(cache, 'unread')).toEqual([]);
});
it('surfaces quota failures instead of claiming drafts are persisted', () => {
  const storage = {
    getItem() {
      return null;
    },
    setItem() {
      throw new DOMException('Full', 'QuotaExceededError');
    }
  } as unknown as Storage;
  expect(() => saveCache(storage, emptyCache('repo'))).toThrow('Full');
});

it('does not silently overwrite a change saved by another tab', () => {
  const cache = loadCache(localStorage, 'repo');
  localStorage.setItem(
    'mdstore:workspace:v1:repo',
    JSON.stringify({ ...cache, summary: 'Other tab' })
  );
  expect(() => saveCache(localStorage, cache)).toThrow('Another tab changed');
});
