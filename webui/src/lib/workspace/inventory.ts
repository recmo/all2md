import type { api, Directory, Page } from './api';

type InventoryApi = Pick<typeof api, 'directory' | 'page'>;

export async function readInventory(api: InventoryApi) {
  async function paths() {
    const directories: Directory[] = [await api.directory()];
    const files: string[] = [];
    for (let index = 0; index < directories.length; index++) {
      for (const child of directories[index].children) {
        if (child.kind === 'directory')
          directories.push(await api.directory(child.path));
        else files.push(child.path);
      }
    }
    return files.sort();
  }
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const before = await paths();
      const pages: Record<string, Page> = {};
      let next = 0;
      await Promise.all(
        Array.from({ length: Math.min(8, before.length) }, async () => {
          while (next < before.length) {
            const path = before[next++];
            pages[path] = await api.page(path);
          }
        })
      );
      if (Object.values(pages).some((page) => !page.exists)) continue;
      const after = await paths();
      if (JSON.stringify(before) !== JSON.stringify(after)) continue;
      const source = JSON.stringify(
        before.map((path) => [path, pages[path].text, pages[path].template])
      );
      const digest = await crypto.subtle.digest(
        'SHA-256',
        new TextEncoder().encode(source)
      );
      const revision = Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, '0')
      ).join('');
      return { pages, paths: before, revision };
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError) throw lastError;
  throw Error(
    'Document tree changed while reading. Retry once changes settle.'
  );
}
