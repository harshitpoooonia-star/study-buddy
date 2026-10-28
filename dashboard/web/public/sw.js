// Study Buddy service worker: app shell cached; API is network-first with an offline fallback.
const SHELL = 'sb-shell-v2'
const API = 'sb-api-v1'
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(['/dash/', '/dash/manifest.webmanifest', '/dash/icon.svg'])))
  self.skipWaiting()
})
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => ![SHELL, API].includes(k)).map((k) => caches.delete(k)))))
  self.clients.claim()
})
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url)
  if (e.request.method !== 'GET' || u.origin !== location.origin || !u.pathname.startsWith('/dash/')) return
  if (u.pathname.startsWith('/dash/ics/') || u.pathname.startsWith('/dash/login') || u.pathname.startsWith('/dash/api/auth')) return
  if (u.pathname.startsWith('/dash/api/')) {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(API).then((c) => c.put(e.request, copy)) }
      return r
    }).catch(() => caches.match(e.request).then((r) => r || new Response('{"offline":true}', { status: 503, headers: { 'content-type': 'application/json' } }))))
    return
  }
  if (u.pathname.startsWith('/dash/assets/')) {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request).then((res) => {
      const copy = res.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); return res
    })))
    return
  }
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(SHELL).then((c) => c.put('/dash/', copy)); return r })
      .catch(() => caches.match('/dash/')))
  }
})
