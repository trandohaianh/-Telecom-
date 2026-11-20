// Service worker tối giản: lưu khung app để mở nhanh / cài lên màn hình chính.
// Dữ liệu API luôn lấy mới từ mạng, không bao giờ trả bản cũ trong cache.
const CACHE = 'la-chan-v6';
const SHELL = ['./', 'index.html', 'theme-init.js', 'app.js', 'theme-toggle.js', 'manifest.webmanifest', 'icon.svg'];

self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Chỉ khung app cùng nguồn; không cache script bên thứ ba (Google) hay phản hồi lỗi (404/500 sẽ bị trả lại khi offline).
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;
  // Khung app: thử mạng trước để luôn có bản mới, mất mạng thì dùng cache.
  e.respondWith(fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); } return r; })
    // Offline deep link (/app/?x, /app/#tab): fall back to the cached app shell instead of the browser error page.
    .catch(() => caches.match(e.request).then(hit => hit || (e.request.mode === 'navigate' ? caches.match('index.html') : undefined))));
});

// Web Push (VAPID): payload chỉ có tiêu đề + một dòng hành động, KHÔNG có số điện thoại hay nội dung SMS.
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Cảnh báo lừa đảo', {
    body: d.body || 'Mở app để xem chi tiết.', icon: 'icon.svg', badge: 'icon.svg', tag: d.tag || 'fraud-alert',
    renotify: true, data: { url: d.url || '/app/' } }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/app/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (new URL(c.url).pathname.startsWith('/app') && 'focus' in c) {
        return ('navigate' in c && c.url !== new URL(target, self.location.origin).href ? c.navigate(target) : Promise.resolve(c))
          .then(w => (w || c).focus());
      }
    }
    return self.clients.openWindow(target);
  }));
});
