import type { Workspace } from '../workspace/state';
import { readonly, type Page } from '../workspace/api';
import { rewriteLinks } from './rewriteLinks';

export function treePaths(workspace: Workspace): string[] {
  return [
    ...new Set([
      ...workspace.paths.filter(
        (path) => !(path in (workspace.deletions || {}))
      ),
      ...Object.keys(workspace.drafts),
      ...(workspace.folders || []).map((path) => path + '/')
    ])
  ].sort();
}
export function validTreePath(path: string): string {
  const parts = path.split('/');
  if (
    !path ||
    path.startsWith('/') ||
    parts.some(
      (part) =>
        !part || part === '.' || part === '..' || part.toLowerCase() === '.git'
    ) ||
    /[\\\x00-\x1f{}]/.test(path)
  )
    throw Error(
      'Enter a repository-relative path without empty components, . or ...'
    );
  return path;
}
export function addFolder(workspace: Workspace, path: string): Workspace {
  validTreePath(path);
  if (
    treePaths(workspace).some(
      (item) =>
        item === path ||
        item.startsWith(path + '/') ||
        path.startsWith(item + '/')
    )
  )
    throw Error('That path already exists or is inside a file.');
  return { ...workspace, folders: [...(workspace.folders || []), path] };
}
export function moveTree(
  workspace: Workspace,
  from: string,
  to: string,
  directory: boolean,
  sources: Record<string, Page>,
  allowTemplates = false,
  allowConfig = false
): Workspace {
  validTreePath(to);
  if (from === to) return workspace;
  if (directory && to.startsWith(from + '/'))
    throw Error('A folder cannot be moved inside itself.');
  const all = treePaths(workspace);
  if (
    all.some(
      (path) =>
        path === to ||
        path.startsWith(to + '/') ||
        (!path.endsWith('/') && to.startsWith(path + '/'))
    )
  )
    throw Error('The destination already exists or is inside a file.');
  const affected = all.filter(
    (path) =>
      !path.endsWith('/') &&
      (path === from || (directory && path.startsWith(from + '/')))
  );
  if (!affected.length && !all.includes(from + '/'))
    throw Error('The source no longer exists.');
  const moves = new Map(
    affected.map((path) => [path, to + path.slice(from.length)])
  );
  for (const [source, destination] of moves) {
    if (
      source === 'config.yaml' ||
      readonly(source, allowTemplates, allowConfig) ||
      readonly(destination, allowTemplates, allowConfig)
    )
      throw Error(`Moving ${source} is not permitted.`);
    if (
      !destination.endsWith('.md') &&
      !/(^|\/)\.?rumdl\.toml$/.test(destination)
    )
      throw Error(
        'Documents must keep a .md extension; lint configurations must keep their rumdl.toml filename.'
      );
  }
  const drafts = { ...workspace.drafts },
    deletions = { ...workspace.deletions };
  for (const path of affected) {
    const source = drafts[path] || sources[path] || workspace.pages[path];
    if (!source) throw Error(`Load ${path} before moving it.`);
    const destination = to + path.slice(from.length);
    if (source.exists) deletions[path] = drafts[path]?.base ?? source.text;
    const original = deletions[destination];
    delete drafts[path];
    delete deletions[destination];
    drafts[destination] = {
      ...source,
      path: destination,
      exists: original !== undefined,
      base: original ?? '',
      template: null
    };
    if (original === source.text) delete drafts[destination];
  }
  for (const path of all.filter((path) => path.endsWith('.md'))) {
    const source =
      workspace.drafts[path] || sources[path] || workspace.pages[path];
    if (!source)
      throw Error(`Load ${path} before moving it so its links can be updated.`);
    const destination = moves.get(path) || path;
    const text = rewriteLinks(source.text, path, destination, moves);
    if (text === source.text) continue;
    if (readonly(path, allowTemplates, allowConfig))
      throw Error(`Updating links in ${path} requires edit permission.`);
    const existing = drafts[destination];
    drafts[destination] = {
      ...source,
      ...existing,
      path: destination,
      base: existing?.base ?? workspace.drafts[path]?.base ?? source.text,
      text
    };
    if (drafts[destination].exists && text === drafts[destination].base)
      delete drafts[destination];
  }
  const mapPath = (path: string) =>
    path === from || (directory && path.startsWith(from + '/'))
      ? to + path.slice(from.length)
      : path;
  return {
    ...workspace,
    drafts,
    deletions,
    folders: [
      ...new Set([
        ...(workspace.folders || []).map(mapPath),
        ...(directory ? [to] : [])
      ])
    ],
    selected: mapPath(workspace.selected)
  };
}

export function deleteTree(
  workspace: Workspace,
  path: string,
  directory: boolean,
  sources: Record<string, Page>,
  allowTemplates = false,
  allowConfig = false
): Workspace {
  validTreePath(path);
  const includes = (item: string) =>
    item === path || (directory && item.startsWith(path + '/'));
  const all = treePaths(workspace);
  const affected = all.filter((item) => !item.endsWith('/') && includes(item));
  if (!affected.length && !all.includes(path + '/'))
    throw Error('The source no longer exists.');
  const drafts = { ...workspace.drafts },
    deletions = { ...workspace.deletions };
  for (const item of affected) {
    if (item === 'config.yaml' || readonly(item, allowTemplates, allowConfig))
      throw Error(`Deleting ${item} is not permitted.`);
    const source = drafts[item] || sources[item] || workspace.pages[item];
    if (!source) throw Error(`Load ${item} before deleting it.`);
    if (source.exists) deletions[item] = drafts[item]?.base ?? source.text;
    delete drafts[item];
  }
  return {
    ...workspace,
    drafts,
    deletions,
    folders: (workspace.folders || []).filter((folder) => !includes(folder)),
    selected: includes(workspace.selected) ? '' : workspace.selected
  };
}
