// Draft Caddie service worker.
// - App shell: cache-first (instant launch, works offline).
// - board.json: network-first, cache fallback (daily updates land; offline OK).
// - Sleeper API: never touched here — app.js fetches it network-only on Refresh.

const VERSION = "v5";
const SHELL = `caddie-shell-${VERSION}`;
const DATA = `caddie-data-${VERSION}`;
const SHELL_FILES = [
  "./",
  "./index.html",
  "./app.js",
  "./style.css",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Only handle same-origin GETs; let Sleeper API calls go straight to network.
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;

  // Network-first with cache fallback for everything (app shell + board.json).
  // Code/board updates always land when online; the cache keeps the app usable offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(DATA).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
