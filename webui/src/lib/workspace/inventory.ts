import type { Api, Directory, Page } from './api';

export async function readInventory(api: Api, cached: Record<string, Page>) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const root = await api.directory();
    try {
      const directories: Directory[] = [root];
      const files: Directory['children'] = [];
      let consistent = true;
      for (let index = 0; index < directories.length; index++) {
        const directory = directories[index];
        if (directory.revision !== root.revision) {
          consistent = false;
          break;
        }
        for (const child of directory.children) {
          if (child.kind === 'directory')
            directories.push(await api.directory(child.path));
          else files.push(child);
        }
      }
      if (!consistent) continue;
      // Schema/config changes also change the template projection on unchanged pages.
      const policy = (path: string) =>
        path === 'config.yaml' ||
        /(^|\/)(schema\.md|\.?rumdl\.toml)$/.test(path);
      const policies = files.filter((file) => policy(file.path));
      const policyChanged =
        policies.some((file) => cached[file.path]?.hash !== file.hash) ||
        Object.keys(cached).some(
          (path) => policy(path) && !policies.some((file) => file.path === path)
        );
      const pages: Record<string, Page> = {};
      let next = 0;
      await Promise.all(
        Array.from({ length: Math.min(8, files.length) }, async () => {
          while (next < files.length) {
            const file = files[next++];
            const prior = cached[file.path];
            // A matching hash permits reuse, even when an unrelated file changed.
            const page =
              !policyChanged && prior?.hash && prior.hash === file.hash
                ? { ...prior, revision: root.revision }
                : await api.page(file.path);
            if (!page.exists || page.revision !== root.revision)
              consistent = false;
            pages[file.path] = page;
          }
        })
      );
      const latest = await api.directory();
      if (
        consistent &&
        latest.revision === root.revision &&
        latest.repository === root.repository
      )
        return { root, pages, paths: files.map((file) => file.path).sort() };
    } catch (error) {
      const latest = await api.directory();
      if (
        latest.revision === root.revision &&
        latest.repository === root.repository
      )
        throw error;
    }
  }
  throw Error(
    'Repository changed while reading documents. Retry once changes settle.'
  );
}
