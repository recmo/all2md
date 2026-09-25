import { describe, it, expect } from 'vitest';
import { emptyCache } from './cache';
import { mergeParts, reconcilePage, resolvePage } from './reconcile';
import type { Page } from './api';
const page = (text: string, exists = true): Page => ({
  path: 'note.md',
  text,
  exists,
  template: null
});
const draft = (base: string, text: string, exists = true) => ({
  ...emptyCache('repo'),
  drafts: { 'note.md': { ...page(text, exists), base } }
});
describe('three-way reconciliation', () => {
  it('merges template title removal with a separate body edit and rebases', () => {
    const base =
      '---\ntitle: Note\nstate: inbox\n---\n# Note\n\nOriginal body.\n';
    const local = base.replace('Original body.', 'Edited body.');
    const server = base.replace('title: Note\n', '');
    const merged = reconcilePage(draft(base, local), page(server));
    expect(merged.conflicts).toEqual({});
    expect(merged.drafts['note.md'].text).toBe(
      server.replace('Original body.', 'Edited body.')
    );
    expect(merged.drafts['note.md'].base).toBe(server);
  });
  it('preserves both versions on conflict and retains nonconflicting regions', () => {
    const cache = reconcilePage(
      draft('a\nb\nc\nd\n', 'mine\nb\nc\nlocal\n'),
      page('theirs\nb\nc\nd\n')
    );
    expect(cache.drafts['note.md'].text).toBe('mine\nb\nc\nlocal\n');
    expect(cache.conflicts!['note.md'].base).toBe('a\nb\nc\nd\n');
    const parts = mergeParts(
      'a\nb\nc\nd\n',
      'mine\nb\nc\nlocal\n',
      'theirs\nb\nc\nd\n'
    );
    expect(parts.map((p) => ('text' in p ? p.text : p.server)).join('')).toBe(
      'theirs\nb\nc\nlocal\n'
    );
    const resolved = resolvePage(
      cache,
      page('theirs\nb\nc\nd\n'),
      'theirs\nb\nc\nlocal\n'
    );
    expect(resolved.conflicts).toEqual({});
    expect(resolved.drafts['note.md'].base).toBe('theirs\nb\nc\nd\n');
  });
  it('handles equal edits, empty text, CRLF, and missing final newlines exactly', () => {
    expect(reconcilePage(draft('old', 'same'), page('same')).drafts).toEqual(
      {}
    );
    expect(mergeParts('a\r\nb\r\nc', 'A\r\nb\r\nc', 'a\r\nb\r\nC')).toEqual([
      { text: 'A\r\nb\r\nC' }
    ]);
    expect(
      reconcilePage(draft('old', ''), page('other')).conflicts!['note.md'].local
    ).toBe('');
  });
  it('requires an explicit choice for delete/modify and restores remote deletions as creates', () => {
    const deleted = { ...emptyCache('repo'), deletions: { 'note.md': 'old' } };
    const conflict = reconcilePage(deleted, page('changed'));
    expect(conflict.deletions!['note.md']).toBe('old');
    expect(conflict.conflicts!['note.md'].local).toBeNull();
    expect(
      resolvePage(conflict, page('changed'), null).deletions!['note.md']
    ).toBe('changed');
    expect(reconcilePage(deleted, page('', false)).deletions).toEqual({});
    const remoteDeleted = reconcilePage(
      draft('old', 'edited'),
      page('', false)
    );
    expect(remoteDeleted.conflicts!['note.md']).toBeDefined();
    expect(
      resolvePage(remoteDeleted, page('', false), 'edited').drafts['note.md']
        .exists
    ).toBe(false);
  });
  it('never overwrites a colliding create and detects another server change after resolution', () => {
    expect(
      reconcilePage(draft('', 'mine', false), page('theirs')).conflicts![
        'note.md'
      ]
    ).toBeDefined();
    const resolved = resolvePage(draft('old', 'mine'), page('theirs'), 'mine');
    expect(
      reconcilePage(resolved, page('newer')).conflicts!['note.md'].base
    ).toBe('theirs');
  });
});
