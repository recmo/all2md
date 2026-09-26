import { validTreePath } from '../navigation/treeEdits';
export type ImportFile = { path: string; file: File };
export function pickedFiles(files: FileList | File[]): ImportFile[] {
  return Array.from(files)
    .filter(
      (file) =>
        !(file.webkitRelativePath || file.name)
          .split('/')
          .some(
            (part) =>
              part === '.git' || part === '.DS_Store' || part.startsWith('._')
          )
    )
    .map((file) => ({
      path: file.webkitRelativePath || file.name,
      file
    }));
}
export async function walkEntry(
  entry: FileSystemEntry,
  parent = ''
): Promise<ImportFile[]> {
  if (
    entry.name === '.git' ||
    entry.name === '.DS_Store' ||
    entry.name.startsWith('._')
  )
    return [];
  const path = parent + entry.name;
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) =>
      (entry as FileSystemFileEntry).file(resolve, reject)
    );
    return [{ path, file }];
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  const files: ImportFile[] = [];
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
      reader.readEntries(resolve, reject)
    );
    if (!batch.length) return files;
    for (const child of batch)
      files.push(...(await walkEntry(child, path + '/')));
  }
}
export function droppedFiles(data: DataTransfer): Promise<ImportFile[]> {
  // Capture entries and files while the drop event's data store is readable.
  const items = Array.from(data.items)
    .filter((item) => item.kind === 'file')
    .map((item) => ({
      entry: item.webkitGetAsEntry?.(),
      file: item.getAsFile()
    }));
  if (!items.length) return Promise.resolve(pickedFiles(data.files));
  return Promise.all(
    items.map(({ entry, file }) =>
      entry
        ? walkEntry(entry)
        : Promise.resolve(file ? [{ path: file.name, file }] : [])
    )
  ).then((groups) => groups.flat());
}
export function importPath(folder: string, path: string): string {
  return validTreePath((folder ? folder.replace(/\/+$/, '') + '/' : '') + path);
}
export function importKind(path: string): 'markdown' | 'asset' | null {
  if (path.endsWith('.md')) return 'markdown';
  return /\.(m4a|mp3|wav|flac|ogg|aac|opus|mp4|mov|mkv|webm|jpg|jpeg|png|gif|webp|avif|svg|pdf|json)$/i.test(
    path
  )
    ? 'asset'
    : null;
}
