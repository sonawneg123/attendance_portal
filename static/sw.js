/* हजेरी पोर्टल — service worker
   नियम: लॉगिन केलेली पाने/डेटा कधीही कॅश करत नाही (दुसऱ्याचा डेटा दिसू नये, व हजेरीची वेळ चुकू नये).
   फक्त स्टॅटिक फाइल्स (CSS, आयकॉन) व ऑफलाइन पान कॅश होते. */
const VERSION = 'v1';
const CACHE = 'hajeri-' + VERSION;
const PRECACHE = ['/offline', '/static/style.css',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;   // POST व बाहेरचे (फॉन्ट) जसे आहेत तसे

  // स्टॅटिक: आधी नेटवर्क (नवीन CSS मिळावी), नेटवर्क नसल्यास कॅश
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req)));
    return;
  }

  // पाने: फक्त नेटवर्क; इंटरनेट नसल्यास मराठी ऑफलाइन पान
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/offline')));
  }
});
