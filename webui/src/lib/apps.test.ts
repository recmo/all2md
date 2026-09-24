import { expect, it } from 'vitest';
import { isApp, appDocuments, applyAppEdit, field } from './apps';
import { emptyCache } from './cache';
it('collections include local drafts and omit staged deletions with explicit missing metadata', () => {
  const cache = emptyCache('repo');
  cache.paths = ['tasks/template.md', 'tasks/a.md', 'missing.md', 'deleted.md'];
  cache.deletions = { 'deleted.md': 'old' };
  cache.drafts = { 'tasks/a.md': { path: 'tasks/a.md', exists: true, base: 'old', template: null, text: '---\nstate: ready\n---\n# Draft title\n' } };
  const result = appDocuments(cache);
  expect(result.documents[0]).toMatchObject({ title: 'Draft title', template: '/tasks/template.md' });
  expect(field(result.documents[0], '/state')).toBe('ready');
  expect(result.missing).toEqual(['missing.md']);
  expect(result.documents.some(d => d.path === 'deleted.md')).toBe(false);
});
it('action updates preserve YAML comments and append inside Timeline', () => {
  const text = '---\nstate: inbox # keep this\ntags: [demo]\n---\n# Task\n\n## Timeline\n\n- Old entry\n\n## Notes\n\nKeep me.\n';
  const result = applyAppEdit(text, { path: 'task.md', fields: { state: 'ready' }, timeline: '2026-09-23T10:00:00Z — Ready.' });
  expect(result).toContain('state: ready # keep this');
  expect(result).toContain('tags: [demo]');
  expect(result).toContain('- Old entry\n- 2026-09-23T10:00:00Z — Ready.\n\n## Notes');
  expect(result).toContain('Keep me.');
});
it('invalid YAML and absent action targets fail without fabricating a timeline', () => {
  expect(() => applyAppEdit('---\nstate: [\n---\n# Task', { path: 'x', fields: { state: 'ready' } })).toThrow();
  expect(() => applyAppEdit('# No timeline', { path: 'x', fields: {}, timeline: 'Event' })).toThrow();
});

it('recognizes app frontmatter independently of filename and excludes definitions from records', () => {
  const source = '---\nmdstore: app\n---\n# Planner\n';
  expect(isApp(source)).toBe(true);
  expect(isApp('# app.md\n')).toBe(false);
  expect(isApp('---\nmdstore: [\n---\n')).toBe(false);
  const cache = emptyCache('repo');
  for (const [path,text] of [['tasks/planner.md',source],['app.md','# Ordinary document\n']]) {
    cache.paths.push(path);
    cache.pages[path] = {path,text,exists:true,template:null};
  }
  expect(appDocuments(cache).documents.map(d => d.path)).toEqual(['app.md']);
});

it('updates nested date bindings while preserving comments and rejecting stale dates', () => {
  const text = '---\nschedule:\n  start: 2026-09-23 # preserve\n  end: 2026-09-25\nother: unchanged\n---\n# Task\n';
  const edit = {path:'task.md',fields:{},expected:{'/schedule/start':'2026-09-23'},pointers:{'/schedule/start':'2026-09-24','/schedule/end':'2026-09-26'}};
  const updated = applyAppEdit(text,edit);
  expect(updated).toContain('start: 2026-09-24 # preserve');
  expect(updated).toContain('end: 2026-09-26');
  expect(updated).toContain('other: unchanged');
  expect(() => applyAppEdit(updated,edit)).toThrow('Task dates changed');
});
