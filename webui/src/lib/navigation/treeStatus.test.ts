import { expect, it } from 'vitest';
import { treeStatuses } from './treeStatus';

it('distinguishes offline copies, drafts, downloads and uncached files', () => {
  const statuses = treeStatuses(
    ['notes/a.md', 'notes/b.md', 'notes/c.md', 'notes/d.md'],
    ['notes/a.md'],
    ['notes/b.md'],
    ['notes/c.md'],
    true
  );
  expect(
    ['a', 'b', 'c', 'd'].map((n) => statuses.get('notes/' + n + '.md')?.text)
  ).toEqual(['✓', '↑', '↓', '○']);
  expect(statuses.get('notes')?.text).toBe('2/4');
  expect(statuses.get('notes/a.md')?.title).toContain('may differ');
});

it('does not promise offline persistence after a failed local save', () => {
  const statuses = treeStatuses(
    ['a.md', 'folder/b.md'],
    ['a.md'],
    ['folder/b.md'],
    [],
    false
  );
  expect([...statuses.values()].every((s) => s.text === '!')).toBe(true);
});

it('shows validity independently of cache status and marks staged deletions', () => {
  const statuses = treeStatuses(
    ['folder/a.md', 'folder/b.md'],
    ['folder/a.md', 'folder/b.md'],
    [],
    [],
    true,
    ['folder/b.md'],
    { valid: false, invalid: ['folder/a.md'], pending: true }
  );
  expect(statuses.get('folder/a.md')?.text).toBe('✓ ×');
  expect(statuses.get('folder/a.md')?.title).toContain('Invalid');
  expect(statuses.get('folder/b.md')?.text).toBe('− ?');
  expect(statuses.get('folder/b.md')?.title).toContain('Staged deletion');
  expect(statuses.get('folder')?.text).toBe('2/2 ×');
  const valid = treeStatuses(['a.md'], ['a.md'], [], [], true, [], {
    valid: true,
    invalid: [],
    pending: false
  });
  expect(valid.get('a.md')?.text).toBe('✓ ●');
  expect(valid.get('a.md')?.title).toContain('Valid');
});
