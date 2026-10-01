// Run with a server using a COPY of dist in /tmp, and a disposable database.
// APP_TEST_URL=http://127.0.0.1:18767 APP_TEST_DIST=/tmp/cvt-shell-test-dist PLAYWRIGHT_MODULE=/path/to/playwright node tests/shell-update.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync, writeFileSync, realpathSync, utimesSync } from "node:fs";
import { join } from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["127.0.0.1", "localhost"].includes(new URL(origin).hostname));
const folder = realpathSync(process.env.APP_TEST_DIST);
assert.ok(folder.startsWith("/private/tmp/") || folder.startsWith("/tmp/"), "Modify only a disposable build copy");
const workerFile = join(folder, "sw.js");
const original = readFileSync(workerFile, "utf8");
let modified = Date.now() - 600000;
const writeWorker = (source) => {
  writeFileSync(workerFile, source);
  // Distinct HTTP Last-Modified seconds avoid a same-second 304 in the fixture.
  modified += 60000;
  utimesSync(workerFile, new Date(modified), new Date(modified));
};
const { version } = JSON.parse(readFileSync(join(folder, "version.json"), "utf8"));
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  // Model the push-only worker shipped before this cache change.
  writeWorker('self.addEventListener("install", () => self.skipWaiting()); self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));');
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const account = await context.request.post(`${origin}/api/auth/register`, { data: {
    username: `shell-${Date.now()}`, password: "local-shell-test-password"
  } });
  assert.equal(account.status(), 200);
  const page = await context.newPage();
  const errors = []; page.on("pageerror", (error) => errors.push(error.message));
  let latest = version, navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) { navigations++; latest = version; }
  });
  await page.route("**/api/version", (route) => route.fulfill({ json: { version: latest } }));
  await page.goto(origin);
  await page.getByRole("button", { name: "开始学习", exact: true }).waitFor();
  await page.waitForFunction(() => navigator.serviceWorker.controller);
  writeWorker(original);
  const migrated = page.waitForEvent("framenavigated", (frame) => frame === page.mainFrame());
  await page.evaluate(async () => (await navigator.serviceWorker.getRegistration()).update());
  await migrated;
  await page.getByRole("button", { name: "开始学习", exact: true }).waitFor();
  await page.waitForFunction(async () => !(await navigator.serviceWorker.getRegistration()).waiting);
  console.log("PASS an existing push-only worker migrates to the offline shell even when the page already has the newest version");
  const next = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/next");
  await page.getByRole("button", { name: "开始学习", exact: true }).click();
  await next;
  await page.locator(".word-meaning").waitFor();
  const answer = page.locator("#study-answer");
  await answer.fill("preserve this draft");
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  assert.equal(await answer.inputValue(), "preserve this draft");
  const updatedCache = `cvt-shell-${version}-update-test`;
  writeWorker(original.replace(`cvt-shell-${version}`, updatedCache));
  await page.evaluate(async () => (await navigator.serviceWorker.getRegistration()).update());
  await page.waitForFunction(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting));
  latest = `${version}-update-test`;
  const before = navigations;
  await page.getByRole("button", { name: "刷新学习数据", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="刷新学习数据"]')?.getAttribute("aria-busy") === "false");
  await page.waitForTimeout(300);
  assert.equal(navigations, before);
  assert.equal(await page.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting)), true);
  await page.getByRole("button", { name: "继续学习", exact: true }).click();
  assert.equal(await answer.inputValue(), "preserve this draft");
  await answer.fill("");
  const reload = page.waitForEvent("framenavigated", (frame) => frame === page.mainFrame());
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await reload;
  await page.getByRole("button", { name: "开始学习", exact: true }).waitFor();
  await page.waitForFunction(async () => !(await navigator.serviceWorker.getRegistration()).waiting);
  assert.ok((await page.evaluate(() => caches.keys())).includes(updatedCache));
  console.log("PASS an installed update waits while a draft exists, then activates and reloads at a safe Home boundary");

  // An incomplete resource bundle must never replace the active worker or reload.
  writeWorker(original.replace(`cvt-shell-${version}`, `cvt-shell-${version}-broken-test`)
    .replace('"/index.html"', '"/missing-shell-resource-test.js","/index.html"'));
  const oldActive = await page.evaluate(async () => {
    const worker = (await navigator.serviceWorker.getRegistration()).active;
    window.__oldActiveWorker = worker; return worker.state;
  });
  assert.equal(oldActive, "activated");
  latest = `${version}-broken-test`;
  const beforeBroken = navigations;
  await page.getByRole("button", { name: "刷新学习数据", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="刷新学习数据"]')?.getAttribute("aria-busy") === "false");
  await page.waitForFunction(async () => !(await navigator.serviceWorker.getRegistration()).installing);
  await page.waitForTimeout(300);
  assert.equal(navigations, beforeBroken);
  assert.equal(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration()).active === window.__oldActiveWorker), true);
  assert.equal(await page.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting)), false);
  await context.setOffline(true); await page.reload();
  await page.getByRole("heading", { name: "离线词义", exact: true }).waitFor();
  console.log("PASS a failed resource download retains the prior worker and its usable offline shell");
  assert.deepEqual(errors, []);
} finally {
  writeFileSync(workerFile, original);
  await browser.close();
}
