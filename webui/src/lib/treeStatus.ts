type StatusBadge = {
  text: string;
  title: string;
  parts?: { text: string; color: string }[];
};

export function treeStatuses(
  paths: string[],
  cached: string[],
  drafts: string[],
  fetching: string[],
  saved: boolean,
  deleted: string[] = [],
  validation?: { valid: boolean; invalid: string[]; pending: boolean },
  conflicts: string[] = []
) {
  const available = new Set([...cached, ...drafts]);
  const pending = new Set([...drafts, ...deleted]);
  const removed = new Set(deleted);
  const loading = new Set(fetching);
  const badges = new Map<string, StatusBadge>();
  const folders = new Map<
    string,
    { total: number; available: number; drafts: number }
  >();
  for (const path of paths) {
    const status = removed.has(path)
      ? { text: '−', title: 'Staged deletion — removed when submitted' }
      : loading.has(path)
        ? { text: '↓', title: 'Downloading latest copy…' }
        : !saved && available.has(path)
          ? {
              text: '!',
              title: 'Local save failed — offline availability is not confirmed'
            }
          : pending.has(path)
            ? {
                text: '↑',
                title:
                  'Local draft — available offline; awaiting validation and submission'
              }
            : available.has(path)
              ? {
                  text: '✓',
                  title:
                    'Available offline — cached copy, may differ from the server'
                }
              : {
                  text: '○',
                  title: 'Not available offline — open or download to cache'
                };
    badges.set(path, status);
    const parts = path.split('/');
    for (let i = 1; i < parts.length; i++) {
      const folder = parts.slice(0, i).join('/');
      const count = folders.get(folder) || {
        total: 0,
        available: 0,
        drafts: 0
      };
      count.total++;
      if (available.has(path)) count.available++;
      if (pending.has(path)) count.drafts++;
      folders.set(folder, count);
    }
  }
  for (const [path, count] of folders) {
    badges.set(path, {
      text:
        !saved && count.available ? '!' : count.available + '/' + count.total,
      title:
        !saved && count.available
          ? 'Local save failed — offline availability is not confirmed'
          : count.available +
            ' of ' +
            count.total +
            ' documents available offline; ' +
            count.drafts +
            ' local drafts awaiting submission'
    });
  }
  const colors: Record<string, string> = {
    '✓': '#246b45', // Saved offline.
    '○': '#69736e', // Not cached.
    '↓': '#2469ad', // Active download.
    '↑': '#946200', // Pending submission.
    '−': '#b43434',
    '!': '#b43434' // Persistence failed.
  };
  for (const [path, badge] of badges) {
    const folder = folders.get(path);
    const color =
      colors[badge.text] ||
      (folder?.drafts
        ? colors['↑']
        : folder?.available === folder?.total
          ? colors['✓']
          : colors['○']);
    badge.parts = [{ text: badge.text, color }];
    if (validation) {
      const invalid = validation.invalid.some(
        (item) => item === path || (folder && item.startsWith(path + '/'))
      );
      const label = invalid
        ? 'Invalid (last completed validation)'
        : validation.valid
          ? 'Valid'
          : validation.pending
            ? 'Validation pending'
            : 'Not validated';
      const mark = invalid ? '×' : validation.valid ? '●' : '?';
      badge.parts.push({
        text: ' ' + mark,
        color: invalid ? '#b43434' : validation.valid ? '#246b45' : '#946200'
      });
      badge.text += ' ' + mark;
      badge.title += '; ' + label;
    }
  }
  for (const [path, badge] of badges) {
    if (conflicts.some(item => item === path || item.startsWith(path + '/'))) {
      badge.text += ' ⚠';
      badge.title += '; Merge conflict — resolve in Submit';
      badge.parts?.push({ text: ' ⚠', color: '#b43434' });
    }
  }
  return badges;
}
