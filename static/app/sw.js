// Service worker tối giản: lưu khung app để mở nhanh / cài lên màn hình chính.
// Dữ liệu API luôn lấy mới từ mạng, không bao giờ trả bản cũ trong cache.
const CACHE = 'la-chan-v3';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icon.svg'];

self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Chỉ khung app cùng nguồn; không cache script bên thứ ba (Google) hay phản hồi lỗi (404/500 sẽ bị trả lại khi offline).
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;
  // Khung app: thử mạng trước để luôn có bản mới, mất mạng thì dùng cache.
  e.respondWith(fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); } return r; })
    .catch(() => caches.match(e.request)));
});
