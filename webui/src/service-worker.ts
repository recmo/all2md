/// <reference lib="webworker" />
import { build, files, version } from '$service-worker';
const worker = self as unknown as ServiceWorkerGlobalScope;
const CACHE = `mdstore-shell-${version}`;
const ASSETS = ['/', '/settings', ...build, ...files];
worker.addEventListener('install', (event) => {
  // Existing tabs must release the old worker before its asset cache is removed.
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
});
worker.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys())
        if (key.startsWith('mdstore-shell-') && key !== CACHE)
          await caches.delete(key);
      await worker.clients.claim();
    })()
  );
});
worker.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== worker.location.origin)
    return;
  const navigation = event.request.mode === 'navigate';
  // Never cache API responses or submission operations.
  if (!ASSETS.includes(url.pathname)) return;
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      if (navigation) {
        try {
          const response = await fetch(event.request, { cache: 'no-cache' });
          if (response.ok) return response;
        } catch {
          // The installed shell remains available offline.
        }
        return (await cache.match('/')) || Response.error();
      }
      return (await cache.match(url.pathname)) || fetch(event.request);
    })()
  );
});
