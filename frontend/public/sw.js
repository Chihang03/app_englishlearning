// Vite fills this list with the complete build. Development keeps push only.
const SHELL_CACHE = "cvt-shell-__BUILD_VERSION__";
const SHELL_URLS = /* PRECACHE */ [];

self.addEventListener("install", (event) => event.waitUntil((async () => {
  if (SHELL_URLS.length) {
    const cache = await caches.open(SHELL_CACHE);
    // A failed download prevents activation of an incomplete offline shell.
    try {
      await cache.addAll(SHELL_URLS.map((url) => new Request(url, { cache: "reload" })));
    } catch (error) {
      await caches.delete(SHELL_CACHE);
      throw error;
    }
  }
  if (self.registration.active) {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) client.postMessage({ type: "APP_SHELL_READY" });
  }
  if (!self.registration.active) await self.skipWaiting();
})()));

self.addEventListener("message", (event) => {
  if (event.data?.type === "ACTIVATE_UPDATE") event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => event.waitUntil((async () => {
  // Track the previously ACTIVE build, not an unused waiting installation.
  // Its assets still belong to pages that are open during the update.
  if (SHELL_URLS.length) try {
    const metadata = await caches.open("cvt-resource-meta-v1");
    const saved = await metadata.match("/__active_shell__");
    const prior = saved ? await saved.json() : null;
    const previous = prior?.current === SHELL_CACHE ? prior.previous : prior?.current;
    await metadata.put("/__active_shell__", new Response(JSON.stringify({ current: SHELL_CACHE, previous })));
    const obsolete = (await caches.keys()).filter((key) => key.startsWith("cvt-shell-") && key !== SHELL_CACHE && key !== previous);
    await Promise.all(obsolete.map((key) => caches.delete(key)));
  } catch { /* Preserve existing resources if storage housekeeping fails. */ }
  await self.clients.claim();
})()));

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (!SHELL_URLS.length || request.method !== "GET" || url.origin !== self.location.origin ||
      url.pathname === "/api" || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate" && (url.pathname === "/" || url.pathname === "/index.html")) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok || response.status < 500) return response;
      } catch { /* Use the complete local build when the connection fails. */ }
      return await (await caches.open(SHELL_CACHE)).match("/index.html") ?? Response.error();
    })());
  } else if (url.pathname.startsWith("/assets/") || SHELL_URLS.includes(url.pathname)) {
    event.respondWith((async () => {
      const cached = await (await caches.open(SHELL_CACHE)).match(url.pathname) ?? await caches.match(url.pathname);
      return cached ?? fetch(request);
    })());
  }
});

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
