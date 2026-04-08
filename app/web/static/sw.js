/**
 * Service Worker — FastAPI Modular Template
 *
 * Strategies:
 *   - API calls  (/api/)           → network-only (never cache)
 *   - Static assets (/static/)     → cache-first, network fallback
 *   - Navigation (HTML pages)      → network-first, offline fallback
 *
 * Each shell asset is cached individually with .catch() so a single
 * CDN failure does not abort the entire install.
 */

const CACHE_VERSION = 'app-v1';
const OFFLINE_URL = '/__offline__';

// Shell assets to pre-cache.  Each is attempted independently — if one
// CDN resource is unreachable the SW still installs successfully.
const SHELL_ASSETS = [
  // Local static assets
  '/static/css/main.css',
  '/static/js/main.js',
  // CDN assets (wrapped individually so failures are tolerated)
  'https://cdn.tailwindcss.com',
  'https://unpkg.com/htmx.org@2.0.4',
  'https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js',
  'https://cdn.jsdelivr.net/npm/chart.js',
];

// ── Offline fallback HTML ────────────────────────────────────────────────────

const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Connecting…</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      display: flex; align-items: center; justify-content: center;
      background-color: #0f172a; color: #cbd5e1;
      font-family: ui-sans-serif, system-ui, sans-serif;
    }
    .card {
      text-align: center; padding: 2.5rem 3rem;
      background: #1e293b; border: 1px solid #334155;
      border-radius: 1rem; max-width: 420px; width: 90%;
    }
    .icon { font-size: 3rem; margin-bottom: 1rem; }
    h1 { font-size: 1.25rem; font-weight: 600; color: #f1f5f9; margin-bottom: 0.5rem; }
    p  { font-size: 0.875rem; color: #94a3b8; margin-bottom: 1.5rem; line-height: 1.6; }
    button {
      display: inline-block; padding: 0.5rem 1.5rem;
      background: #3b82f6; color: #fff;
      border: none; border-radius: 0.5rem;
      font-size: 0.875rem; font-weight: 500;
      cursor: pointer; transition: background 0.15s;
    }
    button:hover { background: #2563eb; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#x1F4E1;</div>
    <h1>Connecting&hellip;</h1>
    <p>You appear to be offline. Check your network connection and try again.</p>
    <button onclick="location.reload()">Retry</button>
  </div>
</body>
</html>`;

// ── Install ──────────────────────────────────────────────────────────────────

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(async (cache) => {
      // Cache the inline offline page first (always succeeds).
      await cache.put(
        new Request(OFFLINE_URL),
        new Response(OFFLINE_HTML, {
          headers: { 'Content-Type': 'text/html; charset=utf-8' },
        })
      );

      // Cache each shell asset independently — a single CDN failure must
      // not cause the entire install to fail.
      await Promise.all(
        SHELL_ASSETS.map((url) =>
          cache.add(url).catch((err) => {
            console.warn('[SW] Failed to pre-cache asset (non-fatal):', url, err);
          })
        )
      );
    })
  );
  // Take control immediately — don't wait for old SW to expire.
  self.skipWaiting();
});

// ── Activate ─────────────────────────────────────────────────────────────────

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_VERSION)
          .map((key) => {
            console.log('[SW] Deleting old cache:', key);
            return caches.delete(key);
          })
      )
    )
  );
  // Claim all open clients immediately.
  self.clients.claim();
});

// ── Fetch ────────────────────────────────────────────────────────────────────

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // 1. API calls — always go to the network; never cache.
  if (url.pathname.startsWith('/api/')) {
    return; // Let the browser handle it normally.
  }

  // 2. Static assets — cache-first, then network, then nothing.
  if (
    url.pathname.startsWith('/static/') ||
    url.hostname !== self.location.hostname // CDN resources
  ) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // 3. Navigation requests (HTML pages) — network-first, offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(networkFirstWithOfflineFallback(request));
    return;
  }

  // 4. Everything else — network-first.
  event.respondWith(networkFirst(request));
});

// ── Strategy helpers ─────────────────────────────────────────────────────────

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Asset not available — return empty 503 rather than throwing.
    return new Response('', { status: 503, statusText: 'Service Unavailable' });
  }
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return cached || new Response('', { status: 503, statusText: 'Service Unavailable' });
  }
}

async function networkFirstWithOfflineFallback(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    // Return the pre-cached offline page.
    return caches.match(OFFLINE_URL);
  }
}
