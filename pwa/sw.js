const CACHE='tomatoiq-shell-v2';
const ASSETS=['/','/assets/index.html','/assets/style.css','/assets/app.js','/assets/manifest.webmanifest','/assets/icon.svg'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{if(event.request.method!=='GET')return;const url=new URL(event.request.url);if(url.pathname.startsWith('/api/')){event.respondWith(fetch(event.request).catch(()=>caches.match(event.request)));return;}event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response;})));});
