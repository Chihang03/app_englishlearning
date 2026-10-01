// Use a local server with a disposable catalog and DATA_DIR.
// APP_TEST_URL=http://127.0.0.1:18764 PLAYWRIGHT_MODULE=/path/to/playwright node tests/meaning-cache.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["localhost", "127.0.0.1"].includes(new URL(origin).hostname), "A disposable local test server is required");
const gate = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const registered = await context.request.post(`${origin}/api/auth/register`, {
    data: { username: `meaning-cache-${Date.now()}`, password: "local-meaning-cache-test" }
  });
  assert.equal(registered.status(), 200);
  const page = await context.newPage();
  const errors = [];
  const calls = [];
  let reviews = 0;
  let holdCached;
  let rejectCached = false;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => { if (new URL(request.url()).pathname === "/api/review") reviews++; });
  await page.route("**/api/study/meanings", async (route) => {
    const body = route.request().postDataJSON();
    calls.push(body);
    if (body.content_cached) {
      if (holdCached) await holdCached.promise;
      if (rejectCached) return route.fulfill({ status: 503, json: { detail: "词义记录暂时失败" } });
    }
    await route.continue();
  });
  const firstCatalogResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/study/meaning-catalog");
  await page.goto(origin);
  assert.equal((await firstCatalogResponse).status(), 200);
  await page.getByRole("button", { name: "开始学习", exact: true }).click();
  const currentCard = (await (await context.request.get(`${origin}/api/next`)).json()).card;
  const openMeanings = async () => {
    await page.locator(".study-menu > summary").click();
    await page.getByRole("button", { name: "更多词义", exact: true }).click();
  };
  const dialog = page.getByRole("dialog", { name: "更多词义" });
  const firstMeaningResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/study/meanings");
  await openMeanings();
  await dialog.locator(".other-sense").first().waitFor();
  await firstMeaningResponse;
  assert.equal(calls.filter((call) => call.content_cached === false).length, 1);
  await dialog.getByRole("button", { name: "关闭" }).click();
  console.log("PASS first opening downloads meanings after recording exposure");

  holdCached = gate();
  const compactRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/study/meanings" && request.postDataJSON().content_cached === true);
  await openMeanings();
  await compactRequest;
  await dialog.locator(".other-sense").first().waitFor();
  assert.equal(await dialog.getByText("正在加载词义…").count(), 0);
  holdCached.resolve();
  holdCached = undefined;
  await dialog.getByRole("button", { name: "关闭" }).click();
  console.log("PASS cached meanings render immediately during exposure upload");

  await page.reload();
  await page.getByRole("button", { name: /开始学习|继续学习/ }).click();
  const afterReload = calls.length;
  await openMeanings();
  await dialog.locator(".other-sense").first().waitFor();
  assert.ok(calls.slice(afterReload).some((call) => call.content_cached === true));
  await dialog.getByRole("button", { name: "关闭" }).click();
  console.log("PASS IndexedDB meanings survive a page reload in the same account");

  rejectCached = true;
  await openMeanings();
  await dialog.getByRole("alert").filter({ hasText: "词义记录暂时失败" }).waitFor();
  assert.ok(await dialog.locator(".other-sense").count() > 0);
  rejectCached = false;
  await dialog.getByRole("button", { name: "重试" }).click();
  await dialog.locator(".other-sense").first().waitFor();
  await dialog.getByRole("button", { name: "关闭" }).click();
  assert.equal((await (await context.request.get(`${origin}/api/next`)).json()).card.attempt_id, currentCard.attempt_id);
  assert.equal((await (await context.request.get(`${origin}/api/next`)).json()).card.answer_exposed, true);
  console.log("PASS failed exposure retains visible cached meanings and retry succeeds");

  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  const catalogCheck = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/study/meaning-catalog");
  await page.getByRole("button", { name: "刷新学习数据" }).click();
  assert.equal((await catalogCheck).status(), 304);
  await page.waitForFunction(() => document.querySelector('[aria-label="刷新学习数据"]')?.getAttribute("aria-busy") === "false");
  await page.getByRole("button", { name: "继续学习", exact: true }).click();
  const before = calls.length;
  await openMeanings();
  await dialog.locator(".other-sense").first().waitFor();
  assert.ok(calls.slice(before).some((call) => call.content_cached === false));
  console.log("PASS manual refresh discards cached meanings and fetches current content");
  console.log("PASS unchanged meaning catalog survives a forced content comparison");

  const second = await context.request.post(`${origin}/api/auth/register`, {
    data: { username: `meaning-other-${Date.now()}`, password: "local-meaning-cache-test" }
  });
  assert.equal(second.status(), 200);
  await page.reload();
  await page.getByRole("button", { name: /开始学习|继续学习/ }).click();
  const newAccountCalls = calls.length;
  await openMeanings();
  await dialog.locator(".other-sense").first().waitFor();
  assert.ok(calls.slice(newAccountCalls).some((call) => call.content_cached === false));
  console.log("PASS a second account cannot use the first account's durable word cache");
  await dialog.getByRole("button", { name: "关闭" }).click();
  holdCached = gate();
  const heldUpload = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/study/meanings" && request.postDataJSON().content_cached === true);
  await openMeanings();
  await heldUpload;
  await dialog.locator(".other-sense").first().waitFor();
  await dialog.getByRole("button", { name: "关闭" }).click();
  const reviewCount = reviews;
  const current = (await (await context.request.get(`${origin}/api/next`)).json()).card;
  await page.locator("#study-answer").fill(current.answer_form);
  await page.locator("#study-answer").press("Enter");
  await page.waitForTimeout(250);
  assert.equal(reviews, reviewCount);
  const reviewRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/review");
  holdCached.resolve();
  holdCached = undefined;
  await reviewRequest;
  console.log("PASS grading waits for the pending cached-meaning exposure upload");
  assert.deepEqual(errors, []);
  console.log("PASS no browser runtime errors");
} finally {
  await browser.close();
}
