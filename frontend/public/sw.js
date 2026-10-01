// Push only: application pages and APIs retain their existing network policy.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data?.json() ?? {}; } catch { /* Show a notification for every push. */ }
  event.waitUntil(self.registration.showNotification(payload.title || "语境学词", {
    body: payload.body || "今天还没有学习，花几分钟学几个单词吧。",
    icon: "/apple-touch-icon.png",
    tag: payload.tag || "learning-reminder",
    data: { url: "/?notification=study#study" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const target = new URL("/?notification=study#study", self.location.origin).href;
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      // Existing app navigation preserves an answer already in progress.
      client.postMessage({ type: "OPEN_STUDY" });
      await client.focus();
      return;
    }
    await self.clients.openWindow(target);
  })());
});
