type StatusBadge = {
  text: string;
  title: string;
  parts?: { text: string; color: string }[];
};

export function treeStatuses(
  paths: string[],
  drafts: string[],
  saved: boolean,
  deleted: string[] = [],
  validation?: { valid: boolean; invalid: string[]; pending: boolean },
  conflicts: string[] = []
) {
  const pending = new Set([...drafts, ...deleted]);
  const removed = new Set(deleted);
  const badges = new Map<string, StatusBadge>();
  const folders = new Map<string, number>();
  for (const path of paths) {
    badges.set(
      path,
      !saved && pending.has(path)
        ? { text: '!', title: 'Local changes could not be saved' }
        : removed.has(path)
          ? { text: '−', title: 'Staged deletion — removed when submitted' }
          : pending.has(path)
            ? { text: '↑', title: 'Local draft — awaiting submission' }
            : { text: '', title: 'Published document' }
    );
    const parts = path.split('/');
    for (let i = 1; i < parts.length; i++) {
      const folder = parts.slice(0, i).join('/');
      folders.set(
        folder,
        (folders.get(folder) || 0) + (pending.has(path) ? 1 : 0)
      );
    }
  }
  for (const [path, count] of folders) {
    badges.set(path, {
      text: !saved && count ? '!' : count ? String(count) : '',
      title:
        !saved && count
          ? 'Local changes could not be saved'
          : `${count} staged changes`
    });
  }
  for (const [path, badge] of badges) {
    const color =
      badge.text === '!' || badge.text === '−' ? '#b43434' : '#946200';
    badge.parts = badge.text ? [{ text: badge.text, color }] : [];
    if (validation) {
      const invalid = validation.invalid.some(
        (item) =>
          item === path || (folders.has(path) && item.startsWith(path + '/'))
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
    if (
      conflicts.some((item) => item === path || item.startsWith(path + '/'))
    ) {
      badge.text += ' ⚠';
      badge.title += '; Merge conflict — resolve in Submit';
      badge.parts?.push({ text: ' ⚠', color: '#b43434' });
    }
  }
  return badges;
}
