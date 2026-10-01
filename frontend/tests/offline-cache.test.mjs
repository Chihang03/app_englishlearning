// APP_TEST_URL=http://127.0.0.1:18766 PLAYWRIGHT_MODULE=/path/to/playwright node tests/offline-cache.test.mjs
// Run only against a local disposable database and catalog.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["localhost", "127.0.0.1"].includes(new URL(origin).hostname), "A disposable local server is required");
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { resolve, promise }; };
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const register = async (prefix) => {
    const response = await context.request.post(`${origin}/api/auth/register`, { data: { username: `${prefix}-${Date.now()}`, password: "local-offline-test-password" } });
    assert.equal(response.status(), 200); return (await response.json()).user;
  };
  const user = await register("offline");
  await context.request.patch(`${origin}/api/settings`, { data: { show_sentence_translation: true } });
  const { card } = await (await context.request.get(`${origin}/api/next`)).json();
  await context.request.post(`${origin}/api/words/${card.id}/mute`);
  let page = await context.newPage();
  const errors = [];
  const consoleErrors = [];
  const attach = () => {
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type()) && !message.text().startsWith("Failed to load resource:")) consoleErrors.push(message.text());
    });
  };
  attach();
  const snapshot = (path) => page.evaluate(async ({ id, path }) => {
    const db = await new Promise((resolve) => { const open = indexedDB.open("cvt-read-snapshots-v1", 1); open.onsuccess = () => resolve(open.result); });
    return await new Promise((resolve) => {
      const get = db.transaction("snapshots").objectStore("snapshots").get(`${id}:${path}`);
      get.onsuccess = () => { resolve(get.result); db.close(); };
    });
  }, { id: user.id, path });
  const settings = () => page.getByRole("button", { name: "设置", exact: true });
  const muted = () => page.getByRole("button", { name: "不再学习的单词", exact: true });
  await page.goto(origin);
  await settings().waitFor();
  await page.waitForFunction(() => navigator.serviceWorker.controller);
  await page.waitForFunction(async (id) => {
    const db = await new Promise((resolve) => { const open = indexedDB.open("cvt-study-content-v1", 2); open.onsuccess = () => resolve(open.result); });
    const saved = await new Promise((resolve) => { const get = db.transaction("catalog").objectStore("catalog").get(id); get.onsuccess = () => resolve(get.result); });
    db.close(); return Boolean(saved);
  }, user.id);
  await settings().click(); await muted().click();
  await page.getByRole("button", { name: `恢复学习 ${card.word}`, exact: true }).waitFor();
  for (let i = 0; i < 30 && !(await snapshot("/api/muted-words")); i++) await page.waitForTimeout(50);
  assert.equal((await snapshot("/api/muted-words")).words[0].word, card.word);

  // Hold fresh server reads after closing the first app window.
  await page.close();
  const hold = deferred();
  const paths = ["stats", "settings", "word-lists", "muted-words"];
  for (const path of paths) await context.route(`**/api/${path}`, async (route) => {
    if (route.request().method() === "GET") await hold.promise;
    await route.continue();
  });
  page = await context.newPage(); attach();
  await page.goto(origin);
  await settings().click();
  await page.waitForFunction(() => [...document.querySelectorAll('label')].find((label) => label.textContent.includes('句子中文翻译'))?.querySelector('input')?.checked === true);
  await muted().click();
  await page.getByRole("button", { name: `恢复学习 ${card.word}`, exact: true }).waitFor({ timeout: 3000 });
  console.log("PASS account-scoped snapshots survive closing the app and render before server reads");
  await context.request.delete(`${origin}/api/muted-words/${encodeURIComponent(card.word)}`);
  hold.resolve();
  await page.getByText("暂无单词", { exact: true }).waitFor();
  for (let i = 0; i < 30 && (await snapshot("/api/muted-words")).words.length; i++) await page.waitForTimeout(50);
  assert.deepEqual((await snapshot("/api/muted-words")).words, []);
  console.log("PASS background validation updates both the UI and the persisted list");
  for (const path of paths) await context.unroute(`**/api/${path}`);

  await page.close(); await context.setOffline(true);
  page = await context.newPage(); attach();
  await page.goto(origin);
  await page.getByRole("heading", { name: "离线词义", exact: true }).waitFor();
  assert.match(await page.title(), /Context/);
  assert.equal(await page.getByRole("button", { name: "开始学习", exact: true }).count(), 0);
  await page.getByRole("searchbox", { name: "搜索缓存单词" }).fill(card.word);
  await page.getByRole("button").filter({ hasText: card.word }).click();
  await page.getByRole("heading", { name: card.word, exact: true }).waitFor();
  assert.ok(await page.locator(".offline-sense").count() > 0);
  await page.screenshot({ path: "/private/tmp/cvt-offline-mobile.png" });
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.screenshot({ path: "/private/tmp/cvt-offline-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  const urls = await page.evaluate(async () => {
    const keys = await caches.keys();
    return (await Promise.all(keys.map(async (key) => (await (await caches.open(key)).keys()).map((request) => new URL(request.url).pathname)))).flat();
  });
  assert.ok(urls.includes("/index.html"));
  assert.equal(urls.some((path) => path.startsWith("/api/")), false);
  console.log("PASS a new offline window opens its shell and stored definitions without caching APIs");
  await context.setOffline(false);
  await page.getByRole("heading", { name: `你好，${user.username}`, exact: true }).waitFor();
  console.log("PASS reconnecting verifies the session before restoring online learning");

  const second = await register("offline-other");
  await context.route("**/api/study/meaning-catalog", (route) => route.abort());
  await page.reload();
  await page.getByRole("heading", { name: `你好，${second.username}`, exact: true }).waitFor();
  await context.setOffline(true); await page.reload();
  await page.getByText("暂无缓存词义", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button").filter({ hasText: card.word }).count(), 0);
  console.log("PASS another account cannot inherit the previous offline library");
  await context.setOffline(false);
  await settings().waitFor(); await settings().click();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await page.getByRole("button", { name: "登录", exact: true }).waitFor();
  await context.setOffline(true); await page.reload();
  await page.getByText("暂时无法连接服务器", { exact: true }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "离线词义", exact: true }).count(), 0);
  console.log("PASS signing out clears the offline identity");
  assert.deepEqual(errors, []);
  assert.deepEqual(consoleErrors, []);
  console.log("PASS no browser runtime errors");
} finally { await browser.close(); }
