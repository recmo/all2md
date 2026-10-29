import type { ValidationSnapshot } from '../validation/localValidation';
import type { Draft, Page } from './api';
export type Cache = {
  version: 1;
  conflicts?: Record<string, import('./reconcile').Conflict>;
  validationSnapshot?: ValidationSnapshot;
  allowTemplateEdits?: boolean;
  allowConfigEdits?: boolean;
  repository: string;
  paths: string[];
  folders?: string[];
  deletions?: Record<string, string>;
  pages: Record<string, Page>;
  drafts: Record<string, Draft>;
  summary: string;
  selected: string;
};
export const emptyCache = (repository: string): Cache => ({
  version: 1,
  repository,
  paths: [],
  pages: {},
  drafts: {},
  summary: '',
  selected: ''
});
const snapshots = new WeakMap<Storage, Map<string, string | null>>();
function observed(storage: Storage) {
  let map = snapshots.get(storage);
  if (!map) {
    map = new Map();
    snapshots.set(storage, map);
  }
  return map;
}
const LAST = 'mdstore:last-repository:v1';
const key = (repository: string) => `mdstore:workspace:v1:${repository}`;
export function loadCache(
  storage: Storage,
  repository = storage.getItem(LAST) || ''
): Cache {
  const raw = repository ? storage.getItem(key(repository)) : null;
  observed(storage).set(repository, raw);
  if (!raw) return emptyCache(repository);
  try {
    const parsed = JSON.parse(raw);
    if (
      parsed.version !== 1 ||
      parsed.repository !== repository ||
      typeof parsed.summary !== 'string' ||
      !parsed.drafts
    )
      throw Error();
    // Treat storage as untrusted; malformed entries must not become edit requests.
    for (const [path, draft] of Object.entries(parsed.drafts) as [
      string,
      Draft
    ][]) {
      if (
        draft.path !== path ||
        typeof draft.text !== 'string' ||
        typeof draft.base !== 'string' ||
        typeof draft.exists !== 'boolean'
      )
        throw Error();
    }
    if (
      parsed.folders &&
      (!Array.isArray(parsed.folders) ||
        parsed.folders.some((path: unknown) => typeof path !== 'string'))
    )
      throw Error();
    if (
      parsed.deletions &&
      (typeof parsed.deletions !== 'object' ||
        Object.values(parsed.deletions).some(
          (base) => typeof base !== 'string'
        ))
    )
      throw Error();
    if (parsed.conflicts) {
      for (const [path, conflict] of Object.entries(parsed.conflicts) as [
        string,
        import('./reconcile').Conflict
      ][]) {
        if (
          !conflict ||
          (conflict.base !== null && typeof conflict.base !== 'string') ||
          (conflict.local !== null && typeof conflict.local !== 'string') ||
          conflict.server?.path !== path ||
          typeof conflict.server.text !== 'string' ||
          typeof conflict.server.exists !== 'boolean' ||
          (conflict.choices &&
            Object.values(conflict.choices).some(
              (value) => typeof value !== 'string'
            ))
        )
          throw Error();
      }
    }
    return { ...parsed, pages: {}, paths: [] };
  } catch {
    throw Error(
      'The saved local changes could not be read. Export or clear browser storage before continuing.'
    );
  }
}
export function saveCache(storage: Storage, cache: Cache) {
  if (!cache.repository) return;
  // Write data before the pointer; quota failures must be surfaced to the editor.
  const current = storage.getItem(key(cache.repository));
  if (current !== (observed(storage).get(cache.repository) ?? null))
    throw Error(
      'Another tab changed this workspace. Export your drafts, then reload before editing further.'
    );
  const { pages, paths, validationSnapshot, ...journal } = cache;
  const encoded = JSON.stringify(journal);
  storage.setItem(key(cache.repository), encoded);
  observed(storage).set(cache.repository, encoded);
  storage.setItem(LAST, cache.repository);
}
export function clearCache(storage: Storage, repository: string) {
  storage.removeItem(key(repository));
  observed(storage).set(repository, null);
  if (storage.getItem(LAST) === repository) storage.removeItem(LAST);
}
