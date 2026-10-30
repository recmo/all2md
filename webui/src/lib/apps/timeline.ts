import { field, type AppDocument } from './apps';

export function calendarDate(value: unknown): Date | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value))
    return null;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year &&
    date.getMonth() === month - 1 &&
    date.getDate() === day
    ? date
    : null;
}

export function timelineData(
  rows: AppDocument[],
  groupBy: string | undefined,
  start: string,
  end: string
) {
  const groups = new Map<string, string>();
  const items: {
    id: string;
    group: string;
    doc: AppDocument;
    start: Date;
    end: Date;
  }[] = [];
  const unscheduled: AppDocument[] = [];
  for (const doc of rows) {
    const value = groupBy ? field(doc, groupBy) : doc.title;
    const name =
      typeof value === 'string' && value.trim()
        ? value.trim()
        : (typeof value === 'number' && Number.isFinite(value)) ||
            typeof value === 'boolean'
          ? String(value)
          : null;
    const group = !groupBy
      ? `document:${doc.path}`
      : name === null
        ? 'unassigned'
        : `group:${JSON.stringify(value)}`;
    groups.set(group, name ?? 'Unassigned');
    const from = calendarDate(field(doc, start)),
      through = calendarDate(field(doc, end));
    if (!from || !through || through < from) {
      unscheduled.push(doc);
      continue;
    }
    // Dates include the final day, including across daylight-saving transitions.
    const until = new Date(through);
    until.setDate(until.getDate() + 1);
    items.push({ id: doc.path, group, doc, start: from, end: until });
  }
  return {
    groups: [...groups]
      .sort((a, b) =>
        !groupBy
          ? 0
          : a[0] === 'unassigned'
            ? 1
            : b[0] === 'unassigned'
              ? -1
              : a[1].localeCompare(b[1])
      )
      .map(([id, name], order) => ({ id, name, order })),
    items,
    unscheduled
  };
}

// Calendar coordinates deliberately avoid elapsed milliseconds across DST.
export const calendarDay = (date: Date) =>
  Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000;
export function ganttLayout(
  model: ReturnType<typeof timelineData>,
  dependencies?: string
) {
  const rows = model.groups
    .filter((group) => model.items.some((item) => item.group === group.id))
    .map((group) => {
      const ends: number[] = [];
      const bars = model.items
        .filter((item) => item.group === group.id)
        .sort((a, b) => +a.start - +b.start || a.id.localeCompare(b.id))
        .map((item) => {
          const from = calendarDay(item.start),
            to = calendarDay(item.end);
          let lane = ends.findIndex((end) => end <= from);
          if (lane < 0) lane = ends.length;
          ends[lane] = to;
          return { ...item, from, to, lane };
        });
      return { ...group, bars, lanes: ends.length };
    });
  const positions = new Map(
    rows.flatMap((row, index) =>
      row.bars.map((bar) => [bar.id, { ...bar, row: index }] as const)
    )
  );
  const links: { source: string; target: string }[] = [];
  const warnings: string[] = [];
  if (dependencies)
    for (const target of positions.values()) {
      const value = field(target.doc, dependencies);
      if (value == null) continue;
      if (
        !Array.isArray(value) ||
        value.some((path) => typeof path !== 'string')
      ) {
        warnings.push(
          `${target.id}: dependencies must be a list of repository paths`
        );
        continue;
      }
      for (const source of new Set<string>(
        value.map((path) => path.replace(/^\//, ''))
      )) {
        if (!positions.has(source) || source === target.id) {
          warnings.push(
            `${target.id}: dependency ${source} is unavailable or refers to itself`
          );
          continue;
        }
        links.push({ source, target: target.id });
      }
    }
  return {
    rows,
    positions,
    links,
    warnings,
    cellHeight: Math.max(1, ...rows.map((row) => row.lanes)) * 36 + 12,
    from: Math.min(...model.items.map((item) => calendarDay(item.start))),
    to: Math.max(...model.items.map((item) => calendarDay(item.end)))
  };
}

export type GanttGesture = 'move' | 'start' | 'end';
export function shiftRange(
  from: number,
  to: number,
  days: number,
  mode: GanttGesture
) {
  return mode === 'move'
    ? { from: from + days, to: to + days }
    : mode === 'start'
      ? { from: Math.min(from + days, to - 1), to }
      : { from, to: Math.max(to + days, from + 1) };
}
export const dateFromDay = (day: number) =>
  new Date(day * 86400000).toISOString().slice(0, 10);
