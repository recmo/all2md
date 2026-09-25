import { diff3Merge } from 'node-diff3';
import type { Page } from './api';
import type { Cache } from './cache';

export type Conflict = { choices?: Record<number, string>; base: string | null; local: string | null; server: Page };
export type MergePart = { text: string } | { yours: string; server: string; base: string };
const lines = (text: string) => text.match(/[^\n]*\n|[^\n]+$/g) || [];
export function mergeParts(base: string, local: string, server: string): MergePart[] {
  return diff3Merge(lines(local), lines(base), lines(server)).map(part =>
    part.ok ? { text: part.ok.join('') } : {
      yours: part.conflict!.a.join(''), server: part.conflict!.b.join(''), base: part.conflict!.o.join('')
    });
}
export function localVersion(cache: Cache, path: string): string | null | undefined {
  return path in (cache.deletions || {}) ? null : cache.drafts[path]?.text;
}
// Pure, atomic cache transformation. Never overwrite unresolved local text.
export function reconcilePage(cache: Cache, page: Page): Cache {
  const path = page.path;
  const local = localVersion(cache, path);
  const conflicts = { ...cache.conflicts };
  delete conflicts[path];
  const next = { ...cache, pages: { ...cache.pages, [path]: page }, conflicts };
  if (local === undefined) return next;
  const draft = cache.drafts[path];
  const base = local === null ? cache.deletions![path] : draft.exists ? draft.base : null;
  const remote = page.exists ? page.text : null;
  if (base === remote) return next;
  if (local === remote) return resolvePage(next, page, remote);
  if (base !== null && local !== null && remote !== null) {
    const parts = mergeParts(base, local, remote);
    if (parts.every(part => 'text' in part))
      return resolvePage(next, page, parts.map(part => 'text' in part ? part.text : '').join(''));
  }
  const previous = cache.conflicts?.[path];
  conflicts[path] = { base, local, server: page,
    choices: previous?.base === base && previous.local === local && previous.server.text === page.text && previous.server.exists === page.exists ? previous.choices : undefined };
  return next;
}
// A deliberate resolution rebases the edit onto the exact fetched server text.
export function resolvePage(cache: Cache, page: Page, text: string | null): Cache {
  const drafts = { ...cache.drafts };
  const deletions = { ...cache.deletions };
  const conflicts = { ...cache.conflicts };
  delete drafts[page.path];
  delete deletions[page.path];
  delete conflicts[page.path];
  if (text === null) {
    if (page.exists) deletions[page.path] = page.text;
  } else if (!page.exists || text !== page.text) {
    drafts[page.path] = { ...page, text, base: page.text };
  }
  return { ...cache, drafts, deletions, conflicts, pages: { ...cache.pages, [page.path]: page } };
}
