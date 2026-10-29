import type { ValidationSnapshot } from '../validation/localValidation';
import type { Draft, Page } from './api';

export type LocalChanges = {
  conflicts?: Record<string, import('./reconcile').Conflict>;
  folders?: string[];
  deletions?: Record<string, string>;
  drafts: Record<string, Draft>;
  summary: string;
  selected: string;
};
export type Workspace = LocalChanges & {
  validationSnapshot?: ValidationSnapshot;
  paths: string[];
  pages: Record<string, Page>;
};
export const emptyWorkspace = (): Workspace => ({
  paths: [],
  pages: {},
  drafts: {},
  summary: '',
  selected: ''
});

// Keep local changes within the current tab and daemon origin.
const KEY = 'mdstore:workspace:v1';
function localChanges(workspace: LocalChanges): LocalChanges {
  const { drafts, deletions, conflicts, folders, summary, selected } =
    workspace;
  return { drafts, deletions, conflicts, folders, summary, selected };
}
export function loadWorkspace(storage: Storage): Workspace {
  const raw = storage.getItem(KEY);
  if (!raw) return emptyWorkspace();
  try {
    const parsed = JSON.parse(raw);
    if (
      typeof parsed.summary !== 'string' ||
      typeof parsed.selected !== 'string' ||
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
    return { ...emptyWorkspace(), ...localChanges(parsed) };
  } catch {
    throw Error(
      'The saved local changes could not be read. Export or clear browser storage before continuing.'
    );
  }
}
export function saveLocalChanges(storage: Storage, workspace: Workspace) {
  storage.setItem(KEY, JSON.stringify(localChanges(workspace)));
}
export function clearLocalChanges(storage: Storage) {
  storage.removeItem(KEY);
}
