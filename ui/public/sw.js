// Mission Control PWA service worker — deliberately minimal.
//
// This is a LIVE dashboard: /events is an SSE stream and /task, /stt, /memory,
// /stats etc. must always hit the network. So this worker only makes the app
// installable and lets the shell (navigations + hashed build assets) open
// offline; it NEVER caches API traffic. Anything that isn't a same-origin GET
// for the shell or a build asset passes straight through, untouched.

const CACHE = "mission-shell-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // POSTs and SSE bypass the worker
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // App-shell navigations: network-first so a fresh dashboard always wins,
  // fall back to the cached shell only when offline.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((res) => {
          caches.open(CACHE).then((c) => c.put("/", res.clone()));
          return res;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Hashed build assets are immutable — cache-first is safe and fast.
  if (url.pathname.startsWith("/assets/")) {
    e.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            caches.open(CACHE).then((c) => c.put(req, res.clone()));
            return res;
          })
      )
    );
  }
  // Everything else (API/GETs, /events) is left to the network by default.
});
