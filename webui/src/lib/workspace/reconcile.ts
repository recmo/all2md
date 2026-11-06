import { diff3Merge } from 'node-diff3';
import type { Page } from './api';
import type { Workspace } from './state';

export type Conflict = {
  choices?: Record<number, string>;
  base: string | null;
  local: string | null;
  server: Page;
};
export type MergePart =
  { text: string } | { yours: string; server: string; base: string };
const lines = (text: string) => text.match(/[^\n]*\n|[^\n]+$/g) || [];
export function mergeParts(
  base: string,
  local: string,
  server: string
): MergePart[] {
  return diff3Merge(lines(local), lines(base), lines(server)).map((part) =>
    part.ok
      ? { text: part.ok.join('') }
      : {
          yours: part.conflict!.a.join(''),
          server: part.conflict!.b.join(''),
          base: part.conflict!.o.join('')
        }
  );
}
export function localVersion(
  workspace: Workspace,
  path: string
): string | null | undefined {
  return path in (workspace.deletions || {})
    ? null
    : workspace.drafts[path]?.text;
}
// Pure, atomic workspace transformation. Never overwrite unresolved local text.
export function reconcilePage(workspace: Workspace, page: Page): Workspace {
  const path = page.path;
  const local = localVersion(workspace, path);
  const conflicts = { ...workspace.conflicts };
  delete conflicts[path];
  const next = {
    ...workspace,
    pages: { ...workspace.pages, [path]: page },
    conflicts
  };
  if (local === undefined) return next;
  const draft = workspace.drafts[path];
  const base =
    local === null
      ? workspace.deletions![path]
      : draft.exists
        ? draft.base
        : null;
  const remote = page.exists ? page.text : null;
  if (base === remote) return next;
  if (local === remote) return resolvePage(next, page, remote);
  if (base !== null && local !== null && remote !== null) {
    const parts = mergeParts(base, local, remote);
    if (parts.every((part) => 'text' in part))
      return resolvePage(
        next,
        page,
        parts.map((part) => ('text' in part ? part.text : '')).join('')
      );
  }
  const previous = workspace.conflicts?.[path];
  conflicts[path] = {
    base,
    local,
    server: page,
    choices:
      previous?.base === base &&
      previous.local === local &&
      previous.server.text === page.text &&
      previous.server.exists === page.exists
        ? previous.choices
        : undefined
  };
  return next;
}
// A deliberate resolution rebases the edit onto the exact fetched server text.
export function resolvePage(
  workspace: Workspace,
  page: Page,
  text: string | null
): Workspace {
  const drafts = { ...workspace.drafts };
  const deletions = { ...workspace.deletions };
  const conflicts = { ...workspace.conflicts };
  delete drafts[page.path];
  delete deletions[page.path];
  delete conflicts[page.path];
  if (text === null) {
    if (page.exists) deletions[page.path] = page.text;
  } else if (!page.exists || text !== page.text) {
    drafts[page.path] = { ...page, text, base: page.text };
  }
  return {
    ...workspace,
    drafts,
    deletions,
    conflicts,
    pages: { ...workspace.pages, [page.path]: page }
  };
}
