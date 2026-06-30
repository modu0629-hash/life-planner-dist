// 생활 플래너 service worker (Phase 1: 앱 셸 캐시 / Phase 2에서 푸시 추가 예정)
const CACHE = "planner-v12";
const SHELL = ["/manifest.webmanifest", "/static/icon.svg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // API는 항상 네트워크 우선 (데이터 신선도)
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(fetch(e.request).catch(() => new Response("{}", { headers: { "Content-Type": "application/json" } })));
    return;
  }
  // 화면(네비게이션)은 항상 최신 받기, 오프라인이면 캐시
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")));
    return;
  }
  // 그 외 정적 자원은 캐시 우선
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});

// Phase 2: 푸시 알림 (월/금 12시 입력알람, 빈 요일 아침알람) 자리
self.addEventListener("push", e => {
  let data = {};
  try { data = e.data.json(); } catch (_) {}
  e.waitUntil(self.registration.showNotification(data.title || "생활 플래너", {
    body: data.body || "", icon: "/static/icon.svg", badge: "/static/icon.svg", data: data
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.openWindow(e.notification.data.url || "/"));
});
