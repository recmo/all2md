import { expect, it } from 'vitest';
import { walkEntry, importPath, importKind, droppedFiles } from './files';
function file(name: string): FileSystemEntry {
  return {
    name,
    isFile: true,
    file: (resolve: (value: File) => void) =>
      resolve(new File(['# Note'], name))
  } as unknown as FileSystemEntry;
}
function directory(
  name: string,
  batches: FileSystemEntry[][]
): FileSystemEntry {
  return {
    name,
    isFile: false,
    createReader: () => {
      let cursor = 0;
      return {
        readEntries: (resolve: (value: FileSystemEntry[]) => void) =>
          resolve(batches[cursor++] || [])
      };
    }
  } as unknown as FileSystemEntry;
}
it('reads every directory batch and preserves nested paths', async () => {
  const root = directory('bundle', [
    Array.from({ length: 100 }, (_, i) => file(`${i}.md`)),
    [
      directory('notes', [[file('last.md')]]),
      directory('.git', [[file('private.json')]])
    ]
  ]);
  const files = await walkEntry(root);
  expect(files).toHaveLength(101);
  expect(files.at(-1)?.path).toBe('bundle/notes/last.md');
});
it('supports file drops without the directory API', async () => {
  const source = new File(['audio'], 'audio.wav');
  const files = await droppedFiles({
    items: [{ kind: 'file', getAsFile: () => source }],
    files: []
  } as unknown as DataTransfer);
  expect(files).toEqual([{ path: 'audio.wav', file: source }]);
});
it('validates destinations and classifies imports without renaming files', () => {
  expect(importPath('meetings/', 'bundle/recording.md')).toBe(
    'meetings/bundle/recording.md'
  );
  for (const path of ['../a.md', '/a.md', '.git/a.md', 'a\\b.md'])
    expect(() => importPath('', path)).toThrow();
  expect(importKind('recording.md')).toBe('markdown');
  expect(importKind('A.MP4')).toBe('asset');
  expect(importKind('capture.json')).toBe('asset');
  expect(importKind('program.exe')).toBeNull();
});
