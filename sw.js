/* オフライン用：通信できるときは常に最新を取得し、圏外のときだけ端末に保存した分を使う */
const PREFIX = 'us-screener-';
const CACHE = PREFIX + 'v2';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/apple-touch-icon.png'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting())));
// 古い保存分を消す（同じサイトにある別のアプリの保存分には触れない）
self.addEventListener('activate', e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith(PREFIX) && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;   // GitHub API などは素通し
  e.respondWith(fetch(req, {cache: 'no-cache'}).then(res => {            // まず通信（更新がすぐ反映される）
    if (res.ok) { const c = res.clone(); caches.open(CACHE).then(x => x.put(req, c)); }
    return res;
  }).catch(async () => (await caches.match(req, {ignoreSearch: true})) ||
    (req.mode === 'navigate' ? caches.match('index.html') : Response.error())));
});
