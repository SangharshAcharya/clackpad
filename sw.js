// Clackpad service worker — app-shell caching so the app opens (and can be
// typed in) with no network connection once it's been visited once.
//
// IMPORTANT: bump CACHE_NAME (e.g. 'clackpad-v4') any time you change any
// file this worker caches (icons, manifest, this file itself). Browsers
// only re-check a service worker file for changes occasionally, and a
// bumped cache name is what guarantees old cached assets actually get
// dropped instead of lingering. index.html itself no longer has this
// problem — see the fetch handler below — but everything in APP_SHELL
// still does.
const CACHE_NAME = 'clackpad-v4';

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

self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;
  // Only handle same-origin requests — let cross-origin things (Google
  // Fonts, the Render API) go straight to the network as normal; caching
  // opaque cross-origin responses adds complexity for little benefit here.
  if (new URL(event.request.url).origin !== location.origin) return;

  var acceptHeader = event.request.headers.get('accept') || '';
  var isHTML = event.request.mode === 'navigate' || acceptHeader.indexOf('text/html') !== -1;

  if (isHTML) {
    // Network-first for the page itself: always try to get whatever is
    // actually live right now, so an update you push shows up on the very
    // next load — no double-refresh, no waiting for a cache-name bump.
    // Only fall back to the cached copy if there's genuinely no network
    // (that's what keeps the app usable offline).
    event.respondWith(
      fetch(event.request).then(function (response) {
        var copy = response.clone();
        caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); });
        return response;
      }).catch(function () { return caches.match(event.request); })
    );
    return;
  }

  // Everything else (icons, manifest, fonts CSS): stale-while-revalidate —
  // these change rarely, so answering instantly from cache while quietly
  // refreshing it in the background is the right tradeoff for them.
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

