// Mission Control PWA service worker — deliberately minimal.
//
// This is a LIVE dashboard: /events is an SSE stream and /task, /stt, /memory,
// /stats etc. must always hit the network. So this worker only makes the app
// installable and lets the shell (navigations + hashed build assets) open
// offline; it NEVER caches API traffic. Anything that isn't a same-origin GET
// for the shell or a build asset passes straight through, untouched.

const CACHE = "mission-shell-v2";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];
// Cap on cached hashed assets. Vite emits content-HASHED filenames, so every
// rebuild adds new entries and nothing ever replaces the old ones — on a phone
// that grows without bound. Cache.keys() is specified to return entries in
// insertion order, so trimming from the front evicts the oldest build first.
const MAX_ASSETS = 60;

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

async function putCapped(req, res) {
  const c = await caches.open(CACHE);
  await c.put(req, res);
  const assets = (await c.keys()).filter((r) =>
    new URL(r.url).pathname.startsWith("/assets/")
  );
  for (const stale of assets.slice(0, Math.max(0, assets.length - MAX_ASSETS))) {
    await c.delete(stale);
  }
}

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
          // ONLY "/", and ONLY when it succeeded (fixed 2026-08-08). This used
          // to store EVERY navigation response under key "/" unconditionally,
          // which poisons the installed app permanently:
          //   - the dashboard's own header links to /system-docs, so one click
          //     cached the docs page as the app shell;
          //   - typing /tasks in the address bar cached raw JSON;
          //   - and behind Tailscale Serve a restarting dispatcher answers 502,
          //     which RESOLVES (so .catch never ran) — the error page became
          //     the shell, and only a later successful navigation to that same
          //     URL could ever replace it.
          if (res.ok && url.pathname === "/") {
            e.waitUntil(putCapped("/", res.clone()));
          }
          return res;
        })
        // caches.match can resolve to undefined, and respondWith(undefined)
        // rejects — which shows a network error instead of the browser's
        // offline page. Response.error() is the honest fallback.
        .catch(async () => (await caches.match("/")) || Response.error())
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
            if (res.ok) e.waitUntil(putCapped(req, res.clone()));
            return res;
          })
      )
    );
  }
  // Everything else (API/GETs, /events) is left to the network by default.
});
