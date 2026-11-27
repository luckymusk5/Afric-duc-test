const CACHE_NAME = 'passage-shell-v1';
const SHELL_FILES = [
  '/',
  '/index.html',
  '/assets/app.js',
  '/assets/app.css',
  '/assets/jsqr.js',
  '/manifest.webmanifest',
  '/offline.html',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_FILES)),
  );
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter(name => name.startsWith('passage-shell-') && name !== CACHE_NAME)
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (
    request.method !== 'GET' ||
    url.origin !== self.location.origin ||
    url.pathname === '/api' ||
    url.pathname.startsWith('/api/')
  ) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3000);
      try {
        return await fetch(request, { signal: controller.signal });
      } catch {
        const cache = await caches.open(CACHE_NAME);
        return await cache.match('/index.html') || await cache.match('/offline.html');
      } finally {
        clearTimeout(timeout);
      }
    })());
    return;
  }

  if (SHELL_FILES.includes(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      return await cache.match(request, { ignoreSearch: true }) || fetch(request);
    })());
  }
});
