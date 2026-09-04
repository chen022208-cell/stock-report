// 盤後快訊 PWA service worker：離線時能看到「最後一次看過」的內容，僅此而已。
// 刻意不做 cache-first——這是每天內容都會變的報告網站，優先永遠是拿最新的，
// 只有真的斷線（fetch 失敗）才退回快取。
const CACHE = "stock-report-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
