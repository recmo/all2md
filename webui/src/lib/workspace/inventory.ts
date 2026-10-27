import type { api, Directory, Page } from './api';

type InventoryApi = Pick<typeof api, 'directory' | 'page'>;

export async function readInventory(
  api: InventoryApi,
  cached: Record<string, Page>
) {
  let lastError: unknown;
  attempts: for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const root = await api.directory();
      const revision = root.revision;
      const directories: Directory[] = [root];
      const paths: string[] = [];
      for (let index = 0; index < directories.length; index++) {
        const directory = directories[index];
        if (directory.revision !== revision) continue attempts;
        for (const child of directory.children) {
          if (child.kind === 'directory')
            directories.push(await api.directory(child.path));
          else paths.push(child.path);
        }
      }
      paths.sort();
      const pages: Record<string, Page> = {};
      let next = 0;
      await Promise.all(
        Array.from({ length: Math.min(8, paths.length) }, async () => {
          while (next < paths.length) {
            const path = paths[next++];
            const prior = cached[path];
            pages[path] =
              prior?.exists && prior.revision === revision
                ? prior
                : await api.page(path);
          }
        })
      );
      if (
        Object.values(pages).some(
          (page) => !page.exists || page.revision !== revision
        )
      )
        continue;
      if ((await api.directory()).revision !== revision) continue;
      return { pages, paths, revision };
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError) throw lastError;
  throw Error(
    'Repository changed while reading documents. Retry once changes settle.'
  );
}
