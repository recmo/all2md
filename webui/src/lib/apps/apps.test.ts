import { expect, it, vi } from 'vitest';
import { isApp, appSources, applyAppEdit, evaluateApp } from './apps';
import { emptyCache } from '../workspace/cache';
it('app sources include drafts, omit staged deletions, and report missing files', () => {
  const cache = emptyCache('repo');
  cache.paths = ['tasks/schema.md', 'tasks/a.md', 'missing.md', 'deleted.md'];
  cache.deletions = { 'deleted.md': 'old' };
  cache.drafts = {
    'tasks/a.md': {
      path: 'tasks/a.md',
      exists: true,
      base: 'old',
      template: null,
      text: '---\nstate: ready\n---\n# Draft title\n'
    }
  };
  const result = appSources(cache);
  expect(result.sources['tasks/a.md']).toContain('# Draft title');
  expect(result.missing).toEqual(['tasks/schema.md', 'missing.md']);
  expect(result.sources['deleted.md']).toBeUndefined();
});
it('action updates preserve YAML comments and append inside Timeline', () => {
  const text =
    '---\nstate: inbox # keep this\ntags: [demo]\n---\n# Task\n\n## Timeline\n\n- Old entry\n\n## Notes\n\nKeep me.\n';
  const result = applyAppEdit(text, {
    path: 'task.md',
    fields: { state: 'ready' },
    timeline: '2026-09-23T10:00:00Z — Ready.'
  });
  expect(result).toContain('state: ready # keep this');
  expect(result).toContain('tags: [demo]');
  expect(result).toContain(
    '- Old entry\n- 2026-09-23T10:00:00Z — Ready.\n\n## Notes'
  );
  expect(result).toContain('Keep me.');
});
it('invalid YAML and absent action targets fail without fabricating a timeline', () => {
  expect(() =>
    applyAppEdit('---\nstate: [\n---\n# Task', {
      path: 'x',
      fields: { state: 'ready' }
    })
  ).toThrow();
  expect(() =>
    applyAppEdit('# No timeline', { path: 'x', fields: {}, timeline: 'Event' })
  ).toThrow();
});

it('recognizes app frontmatter and supplies definitions to the Rust projection', () => {
  const source = '---\nmdstore: app\n---\n# Planner\n';
  expect(isApp(source)).toBe(true);
  expect(isApp('# app.md\n')).toBe(false);
  expect(isApp('---\nmdstore: [\n---\n')).toBe(false);
  const cache = emptyCache('repo');
  for (const [path, text] of [
    ['tasks/planner.md', source],
    ['app.md', '# Ordinary document\n']
  ]) {
    cache.paths.push(path);
    cache.pages[path] = { path, text, exists: true, template: null };
  }
  expect(Object.keys(appSources(cache).sources)).toEqual([
    'tasks/planner.md',
    'app.md'
  ]);
});

it('updates nested date bindings while preserving comments and rejecting stale dates', () => {
  const text =
    '---\nschedule:\n  start: 2026-09-23 # preserve\n  end: 2026-09-25\nother: unchanged\n---\n# Task\n';
  const edit = {
    path: 'task.md',
    fields: {},
    expected: { '/schedule/start': '2026-09-23' },
    pointers: { '/schedule/start': '2026-09-24', '/schedule/end': '2026-09-26' }
  };
  const updated = applyAppEdit(text, edit);
  expect(updated).toContain('start: 2026-09-24 # preserve');
  expect(updated).toContain('end: 2026-09-26');
  expect(updated).toContain('other: unchanged');
  expect(() => applyAppEdit(updated, edit)).toThrow('Task dates changed');
});

it('app sources use current text instead of stale snapshot metadata', () => {
  const cache = emptyCache('repo');
  cache.paths = ['task.md'];
  cache.validationSnapshot = {
    version: 1,
    revision: 'old',
    files: {},
    edges: [],
    documents: {
      'task.md': {
        hash: 'old',
        parsed: {
          frontmatter: { mdstore: 'app', state: 'inbox' },
          headings: [{ level: 1, text: 'Old' }]
        }
      }
    }
  } as unknown as NonNullable<typeof cache.validationSnapshot>;
  cache.pages['task.md'] = {
    path: 'task.md',
    exists: true,
    template: null,
    text: '---\nstate: ready\n---\n# Fresh\n'
  };
  expect(appSources(cache).sources['task.md']).toBe(
    cache.pages['task.md'].text
  );
  cache.pages['task.md'].text = '---\nmdstore: app\n---\n# Planner\n';
  expect(appSources(cache).sources['task.md']).toBe(
    cache.pages['task.md'].text
  );
});

it('aborting app evaluation terminates its worker and clears its timeout', async () => {
  const terminate = vi.fn();
  const postMessage = vi.fn();
  vi.useFakeTimers();
  vi.stubGlobal(
    'Worker',
    class {
      terminate = terminate;
      postMessage = postMessage;
    }
  );
  try {
    const controller = new AbortController();
    const pending = evaluateApp(
      'app.md',
      '',
      {},
      undefined,
      undefined,
      controller.signal
    );
    const rejected = expect(pending).rejects.toMatchObject({
      name: 'AbortError'
    });
    await Promise.resolve();
    expect(postMessage).toHaveBeenCalledOnce();
    controller.abort();
    await rejected;
    expect(terminate).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    await expect(
      evaluateApp('app.md', '', {}, undefined, undefined, controller.signal)
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(postMessage).toHaveBeenCalledOnce();
  } finally {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  }
});
