// Run against a local test server with a disposable database, never production:
// APP_TEST_URL=http://127.0.0.1:18764 PLAYWRIGHT_MODULE=/path/to/playwright node tests/manual-sync.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["localhost", "127.0.0.1"].includes(new URL(origin).hostname), "A local disposable test server is required");
const { version } = JSON.parse(readFileSync(new URL("../src/version.json", import.meta.url), "utf8"));
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await context.addInitScript(() => {
    Object.defineProperty(window.speechSynthesis, "getVoices", { value: () => [
      { voiceURI: "test-en", name: "Test English", lang: "en-US", default: true, localService: true }
    ] });
    Object.defineProperty(window.speechSynthesis, "cancel", { value: () => {} });
  });
  const registered = await context.request.post(`${origin}/api/auth/register`, {
    data: { username: `sync-ui-${Date.now()}`, password: "local-sync-test-password" }
  });
  assert.equal(registered.status(), 200);
  const { user } = await registered.json();
  assert.equal((await context.request.patch(`${origin}/api/settings`, { data: { speech_rate: 120 } })).status(), 200);
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  let navigations = 0;
  let latest = version;
  let rejectUploads = false;
  let uploadGate;
  let rejectStats = false;
  let listsGate;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) navigations++;
    if (new URL(request.url()).pathname.startsWith("/api/")) requests.push({
      path: new URL(request.url()).pathname, method: request.method(), body: request.postDataJSON()
    });
  });
  await page.route("**/api/version", (route) => route.fulfill({ json: { version: latest } }));
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PATCH") {
      if (rejectUploads) return route.fulfill({ status: 503, json: { detail: "语速上传暂时失败" } });
      if (uploadGate) await uploadGate.promise;
    }
    await route.continue();
  });
  await page.route("**/api/stats", (route) => rejectStats
    ? route.fulfill({ status: 503, json: { detail: "学习数据暂时不可用" } }) : route.continue());
  await page.route("**/api/word-lists", async (route) => {
    if (listsGate) await listsGate.promise;
    await route.continue();
  });
  const refresh = page.getByRole("button", { name: "刷新学习数据", exact: true });
  const settingsButton = page.getByRole("button", { name: "设置", exact: true });
  const back = page.getByRole("button", { name: "返回首页", exact: true });
  const speed = (name) => page.getByRole("group", { name: "语速", exact: true }).getByRole("button", { name, exact: true });
  const saved = async (rate) => page.waitForFunction(({ id, rate }) => {
    const cached = JSON.parse(localStorage.getItem(`cvt.speech.rate.${id}`));
    return cached?.rate === rate && cached.pending === false;
  }, { id: user.id, rate });
  const response = (path, method = "GET") => page.waitForResponse((r) => new URL(r.url()).pathname === path && r.request().method() === method);
  const finish = () => page.waitForFunction(() => document.querySelector('[aria-label="刷新学习数据"]')?.getAttribute("aria-busy") === "false");
  await page.goto(origin);
  await settingsButton.waitFor();
  await saved(120);

  // Another device changed settings while this page still has fresh read caches.
  await context.request.patch(`${origin}/api/settings`, { data: { speech_rate: 175, show_sentence_translation: true } });
  requests.length = 0;
  await refresh.click();
  await finish();
  for (const path of ["/api/auth/me", "/api/stats", "/api/settings", "/api/word-lists", "/api/version"]) {
    assert.equal(requests.filter((r) => r.path === path && r.method === "GET").length, 1, path);
  }
  assert.equal(requests.filter((r) => r.path === "/api/next").length, 0);
  await settingsButton.click();
  assert.equal(await speed("快").getAttribute("aria-pressed"), "true");
  assert.equal(await page.getByRole("checkbox", { name: "句子中文翻译", exact: true }).isChecked(), true);
  console.log("PASS forced refresh bypasses fresh caches and version throttle; remote settings applied without creating a question");

  rejectUploads = true;
  const failedSelection = response("/api/settings", "PATCH");
  await speed("慢").click();
  assert.equal((await failedSelection).status(), 503);
  await back.click();
  requests.length = 0;
  await refresh.click();
  await page.getByRole("alert").filter({ hasText: "语速上传暂时失败" }).waitFor();
  await finish();
  assert.equal(requests.filter((r) => ["/api/stats", "/api/word-lists", "/api/version"].includes(r.path)).length, 0);
  assert.deepEqual(await page.evaluate((id) => JSON.parse(localStorage.getItem(`cvt.speech.rate.${id}`)), user.id), { rate: 90, pending: true });
  console.log("PASS failed forced upload surfaces an error and retains the pending local selection");

  rejectUploads = false;
  uploadGate = deferred();
  requests.length = 0;
  const pendingUpload = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/settings" && r.method() === "PATCH");
  await refresh.click();
  await pendingUpload;
  assert.equal(await refresh.isDisabled(), true);
  assert.match(await refresh.innerText(), /刷新中/);
  await refresh.evaluate((button) => { button.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  assert.equal(requests.filter((r) => r.path === "/api/auth/me").length, 1);
  assert.equal(requests.filter((r) => ["/api/stats", "/api/word-lists", "/api/version"].includes(r.path)).length, 0);
  uploadGate.resolve();
  uploadGate = undefined;
  await finish();
  await saved(90);
  assert.equal(await page.getByRole("alert").count(), 0);
  const uploadIndex = requests.findIndex((r) => r.method === "PATCH");
  assert.ok(uploadIndex > requests.findIndex((r) => r.path === "/api/auth/me"));
  for (const path of ["/api/stats", "/api/settings", "/api/word-lists", "/api/version"]) {
    assert.ok(requests.findIndex((r) => r.path === path && r.method === "GET") > uploadIndex, path);
  }
  assert.equal((await (await context.request.get(`${origin}/api/settings`)).json()).speech_rate, 90);
  console.log("PASS retry waits for upload acknowledgement before downloads and ignores duplicate refresh clicks");

  // A no-op flush must not leave the speech writer permanently locked.
  await refresh.click();
  await finish();
  await settingsButton.click();
  await speed("正常").click();
  await saved(120);
  uploadGate = deferred();
  const firstUpload = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/settings" && r.method() === "PATCH");
  await speed("慢").click();
  await firstUpload;
  await speed("快").click();
  uploadGate.resolve();
  uploadGate = undefined;
  await saved(175);
  assert.equal((await (await context.request.get(`${origin}/api/settings`)).json()).speech_rate, 175);
  console.log("PASS subsequent speech changes still upload; rapid choices save the newest rate last");
  await back.click();

  rejectStats = true;
  listsGate = deferred();
  const failedDownload = response("/api/stats");
  await refresh.click();
  assert.equal((await failedDownload).status(), 503);
  assert.equal(await refresh.isDisabled(), true);
  listsGate.resolve();
  listsGate = undefined;
  await finish();
  await page.getByRole("alert").filter({ hasText: "学习数据暂时不可用" }).waitFor();
  rejectStats = false;
  await refresh.click();
  await finish();
  assert.equal(await page.getByRole("alert").count(), 0);
  console.log("PASS failed download waits for remaining reads and can be retried");

  const nextCard = response("/api/next");
  await page.getByRole("button", { name: "开始学习", exact: true }).click();
  const card = (await (await nextCard).json()).card;
  const answer = page.getByRole("textbox", { name: "输入英文答案", exact: true });
  await answer.fill("unfinished draft");
  await back.click();
  latest = `${version}-test-update`;
  const beforeRefresh = navigations;
  requests.length = 0;
  await refresh.click();
  await finish();
  assert.equal(navigations, beforeRefresh);
  assert.equal(requests.filter((r) => r.path === "/api/next").length, 0);
  await page.getByRole("button", { name: "继续学习", exact: true }).click();
  assert.equal(await answer.inputValue(), "unfinished draft");
  assert.equal((await (await context.request.get(`${origin}/api/next`)).json()).card.attempt_id, card.attempt_id);
  await answer.fill("");
  latest = version;
  const safeReload = page.waitForEvent("request", (r) => r.isNavigationRequest() && r.frame() === page.mainFrame());
  await back.click();
  await safeReload;
  await settingsButton.waitFor();
  console.log("PASS forced version check preserves a draft and applies an update at a safe Home boundary");

  await settingsButton.click();
  rejectUploads = true;
  const oldAccountWrite = response("/api/settings", "PATCH");
  await speed("慢").click();
  await oldAccountWrite;
  await back.click();
  const second = await context.request.post(`${origin}/api/auth/register`, {
    data: { username: `sync-other-${Date.now()}`, password: "local-sync-test-password" }
  });
  assert.equal(second.status(), 200);
  const other = (await second.json()).user;
  rejectUploads = false;
  requests.length = 0;
  await refresh.click();
  await page.getByRole("heading", { name: `你好，${other.username}`, exact: true }).waitFor();
  await page.waitForFunction((id) => JSON.parse(localStorage.getItem(`cvt.speech.rate.${id}`))?.pending === false, other.id);
  assert.equal(requests.filter((r) => r.method === "PATCH" && r.body?.speech_rate === 90).length, 0);
  assert.equal(await page.evaluate((id) => JSON.parse(localStorage.getItem(`cvt.speech.rate.${id}`)).pending, user.id), true);
  console.log("PASS changed session remounts the account before uploading and preserves the old account's outbox");
  assert.deepEqual(errors, []);
  console.log("PASS no browser runtime errors");
} finally {
  await browser.close();
}
