// Clackpad service worker — app-shell caching so the app opens (and can be
// typed in) with no network connection once it's been visited once.
//
// IMPORTANT: bump CACHE_NAME (e.g. 'clackpad-v2') any time you deploy a real
// update to index.html. Browsers keep using a cached service worker/cache
// until the cache name changes, so without bumping this, people could keep
// seeing an old version indefinitely instead of your update.
const CACHE_NAME = 'clackpad-v3';

const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-512-maskable.png',
  './icons/apple-touch-icon.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) { return cache.addAll(APP_SHELL); })
      .catch(function () { /* fine if an icon 404s in dev; don't block install */ })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE_NAME; })
            .map(function (k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

// Stale-while-revalidate: answer instantly from cache when we have it (so
// it works offline and feels instant), but also fetch a fresh copy in the
// background to update the cache for next time — so people aren't stuck on
// a stale version forever just because the cache name didn't change yet.
self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;
  // Only handle same-origin requests — let cross-origin things (Google
  // Fonts, the Render API) go straight to the network as normal; caching
  // opaque cross-origin responses adds complexity for little benefit here.
  if (new URL(event.request.url).origin !== location.origin) return;

  event.respondWith(
    caches.match(event.request).then(function (cached) {
      var network = fetch(event.request).then(function (response) {
        if (response && response.status === 200) {
          var copy = response.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); });
        }
        return response;
      }).catch(function () { return cached; });
      return cached || network;
    })
  );
});
