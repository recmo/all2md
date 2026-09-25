import { expect, it } from 'vitest';
import { timelineData } from './timeline';
import type { AppDocument } from './apps';
const doc = (
  path: string,
  frontmatter: Record<string, unknown>
): AppDocument => ({
  path,
  title: path,
  frontmatter,
  template: null,
  text: null
});
it('groups overlapping tasks by resource and includes the final calendar day', () => {
  const model = timelineData(
    [
      doc('a.md', { who: 'Alice', from: '2026-09-23', through: '2026-09-25' }),
      doc('b.md', { who: 'Alice', from: '2026-09-24', through: '2026-09-24' }),
      doc('c.md', { who: 'Bob', from: '2026-09-26', through: '2026-09-27' })
    ],
    '/who',
    '/from',
    '/through'
  );
  expect(model.groups.map((g) => g.name)).toEqual(['Alice', 'Bob']);
  expect(model.items[0].group).toBe(model.items[1].group);
  expect(model.items[0].end.getDate()).toBe(26);
  expect(model.items[1].end.getDate()).toBe(25);
});
it('keeps unassigned tasks distinct and reports invalid or missing dates', () => {
  const model = timelineData(
    [
      doc('a.md', { from: '2026-09-23', through: '2026-09-23' }),
      doc('b.md', {
        who: 'Unassigned',
        from: '2026-09-23',
        through: '2026-09-23'
      }),
      doc('bad.md', {
        who: 'Alice',
        from: '2026-02-30',
        through: '2026-03-01'
      }),
      doc('backwards.md', {
        who: 'Alice',
        from: '2026-09-25',
        through: '2026-09-23'
      }),
      doc('missing.md', { who: 'Alice' })
    ],
    '/who',
    '/from',
    '/through'
  );
  expect(model.items).toHaveLength(2);
  expect(model.items[0].group).not.toBe(model.items[1].group);
  expect(model.unscheduled.map((d) => d.path)).toEqual([
    'bad.md',
    'backwards.md',
    'missing.md'
  ]);
});

it('ungrouped timelines give each task its own row in collection order', () => {
  const a = doc('z.md', { from: '2026-09-23', through: '2026-09-24' });
  const b = { ...a, path: 'a.md' };
  const model = timelineData([a, b], undefined, '/from', '/through');
  expect(model.groups.map((g) => g.id)).toEqual([
    'document:z.md',
    'document:a.md'
  ]);
  expect(model.items.map((i) => i.group)).toEqual(
    model.groups.map((g) => g.id)
  );
});

it('packs lanes, reuses lanes after inclusive end dates, and resolves path dependencies', async () => {
  const { ganttLayout } = await import('./timeline');
  const model = timelineData(
    [
      doc('a.md', { who: 'Alice', from: '2026-09-23', through: '2026-09-25' }),
      doc('b.md', {
        who: 'Alice',
        from: '2026-09-24',
        through: '2026-09-24',
        deps: ['/a.md']
      }),
      doc('c.md', {
        who: 'Alice',
        from: '2026-09-26',
        through: '2026-09-26',
        deps: ['b.md', 'b.md']
      }),
      doc('d.md', {
        who: 'Bob',
        from: '2026-09-26',
        through: '2026-09-27',
        deps: ['c.md', 'missing.md']
      })
    ],
    '/who',
    '/from',
    '/through'
  );
  const layout = ganttLayout(model, '/deps');
  expect(layout.rows[0].bars.map((bar) => bar.lane)).toEqual([0, 1, 0]);
  expect(layout.cellHeight).toBe(84);
  expect(layout.links).toEqual([
    { source: 'a.md', target: 'b.md' },
    { source: 'b.md', target: 'c.md' },
    { source: 'c.md', target: 'd.md' }
  ]);
  expect(layout.warnings).toHaveLength(1);
  expect(layout.positions.get('d.md')?.row).toBe(1);
});

it('moves calendar dates across DST and clamps either resized edge to a single day', async () => {
  const { shiftRange, calendarDay, dateFromDay } = await import('./timeline');
  const from = calendarDay(new Date(2026, 9, 24)),
    to = calendarDay(new Date(2026, 9, 27));
  const moved = shiftRange(from, to, 2, 'move');
  expect(dateFromDay(moved.from)).toBe('2026-10-26');
  expect(dateFromDay(moved.to - 1)).toBe('2026-10-28');
  expect(shiftRange(from, to, 100, 'start')).toEqual({ from: to - 1, to });
  expect(shiftRange(from, to, -100, 'end')).toEqual({ from, to: from + 1 });
});
