import type { Cache } from './cache';
import type { Page } from './api';

// Published documents are replaceable. Keep them out of the synchronous draft
// journal, and write only changed records rather than serializing the corpus.
const database = 'mdstore-documents';
let opening: Promise<IDBDatabase> | undefined;
function open(): Promise<IDBDatabase> {
  if (!opening) {
    opening = new Promise((resolve, reject) => {
      const request = indexedDB.open(database, 1);
      request.onupgradeneeded = () =>
        request.result.createObjectStore('documents');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    void opening.catch(() => {
      opening = undefined;
    });
  }
  return opening;
}
function completed(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () =>
      reject(transaction.error || Error('Offline cache transaction aborted'));
    transaction.onerror = () => reject(transaction.error);
  });
}
const previous = new Map<string, Cache>();
const committed = new Map<string, Cache>();
let writes = Promise.resolve();
export function saveDocuments(cache: Cache): Promise<void> {
  const before = previous.get(cache.repository);
  if (
    !cache.repository ||
    (before?.pages === cache.pages &&
      before.paths === cache.paths &&
      before.validationSnapshot === cache.validationSnapshot)
  )
    return writes;
  previous.set(cache.repository, cache);
  const write = writes
    .catch(() => {})
    .then(async () => {
      const before = committed.get(cache.repository);
      const db = await open();
      const transaction = db.transaction('documents', 'readwrite');
      const done = completed(transaction);
      const store = transaction.objectStore('documents');
      for (const [path, page] of Object.entries(cache.pages)) {
        if (before?.pages[path] !== page)
          store.put(page, [cache.repository, path]);
      }
      for (const path of Object.keys(before?.pages || {})) {
        if (!(path in cache.pages)) store.delete([cache.repository, path]);
      }
      if (
        before?.paths !== cache.paths ||
        before?.validationSnapshot !== cache.validationSnapshot
      )
        store.put({ paths: cache.paths, snapshot: cache.validationSnapshot }, [
          cache.repository,
          ''
        ]);
      await done;
      committed.set(cache.repository, cache);
    });
  writes = write;
  return write;
}
export async function loadDocuments(cache: Cache): Promise<Cache> {
  await writes.catch(() => {});
  const db = await open();
  const transaction = db.transaction('documents', 'readonly');
  const done = completed(transaction);
  const store = transaction.objectStore('documents');
  const pages: Record<string, Page> = {};
  let metadata:
    { paths: string[]; snapshot?: Cache['validationSnapshot'] } | undefined;
  const request = store.openCursor(
    IDBKeyRange.bound([cache.repository, ''], [cache.repository, '\uffff'])
  );
  request.onsuccess = () => {
    const cursor = request.result;
    if (!cursor) return;
    const path = (cursor.key as string[])[1];
    if (path) pages[path] = cursor.value;
    else metadata = cursor.value;
    cursor.continue();
  };
  await done;
  const restored = metadata
    ? {
        ...cache,
        paths: metadata.paths,
        pages,
        validationSnapshot: metadata.snapshot
      }
    : cache;
  committed.set(cache.repository, restored);
  return restored;
}
export function clearDocuments(repository: string): Promise<void> {
  previous.delete(repository);
  committed.delete(repository);
  const clear = writes
    .catch(() => {})
    .then(async () => {
      const db = await open();
      const transaction = db.transaction('documents', 'readwrite');
      const done = completed(transaction);
      transaction
        .objectStore('documents')
        .delete(IDBKeyRange.bound([repository, ''], [repository, '\uffff']));
      await done;
      committed.delete(repository);
    });
  writes = clear;
  return clear;
}
