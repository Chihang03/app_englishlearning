// APP_TEST_URL=http://127.0.0.1:18771 PLAYWRIGHT_MODULE=/path/to/playwright node tests/learning-calendar.test.mjs
// Run only against a disposable local database.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["127.0.0.1", "localhost"].includes(new URL(origin).hostname));
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const registered = await context.request.post(`${origin}/api/auth/register`, { data: {
    username: `calendar-${Date.now()}`, password: "local-calendar-test-password", timezone: "Asia/Shanghai"
  } });
  assert.equal(registered.status(), 200);
  const next = await context.request.get(`${origin}/api/next`);
  const { card } = await next.json();
  for (const [answer, milliseconds] of [["wrong", 6200], [card.answer_form, 1800]]) {
    const response = await context.request.post(`${origin}/api/review`, { data: {
      word_id: card.id, sense_id: card.sense_id, example_id: card.example_id,
      attempt_id: card.attempt_id, user_answer: answer, active_response_ms: milliseconds
    } });
    assert.equal(response.status(), 200);
  }
  const expected = await (await context.request.get(`${origin}/api/learning-calendar`)).json();
  const page = await context.newPage();
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto(origin);
  await page.getByRole("button", { name: "学习日历", exact: true }).waitFor();
  assert.equal(await page.locator(".calendar-grid").count(), 0);
  await page.getByRole("button", { name: "学习日历", exact: true }).click();
  await page.locator(".calendar-grid").waitFor();
  assert.equal(new URL(page.url()).hash, "#learning-calendar");
  assert.equal(await page.locator(".calendar-day").count(), expected.days.length);
  const today = page.locator(`[aria-current="date"]`);
  assert.equal(await today.getAttribute("aria-pressed"), "true");
  assert.equal(await page.getByRole("button", { name: "下个月", exact: true }).isDisabled(), true);
  const results = page.getByRole("region", { name: "当天成果" });
  assert.match(await results.innerText(), /1 张/);
  assert.match(await results.innerText(), /8秒/);
  assert.match(await results.innerText(), /0%/);
  assert.match(await page.getByRole("region", { name: "本月汇总" }).innerText(), /学习 1 天/);
  await page.screenshot({ path: "/tmp/englishlearning-calendar-390.png", fullPage: true });
  for (const width of [320, 390, 430]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `No horizontal overflow at ${width}px`);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "上个月", exact: true }).click();
  const priorDate = new Date(`${expected.month}-01T00:00:00Z`);
  priorDate.setUTCMonth(priorDate.getUTCMonth() - 1);
  const previousFirst = `${priorDate.toISOString().slice(0, 7)}-01`;
  await page.getByRole("button", { name: `${previousFirst}，完成 0 张卡片`, exact: true }).waitFor();
  const previous = page.locator(".calendar-day[aria-pressed=true]");
  assert.match(await previous.getAttribute("aria-label"), /-01，/);
  assert.match(await results.innerText(), /0 张/);
  await page.getByRole("button", { name: "下个月", exact: true }).click();
  await today.waitFor();
  assert.equal(await today.getAttribute("aria-pressed"), "true");
  if (Number(expected.today.slice(-2)) > 1) {
    const earlier = page.locator(".calendar-day").first();
    await earlier.click();
    assert.equal(await earlier.getAttribute("aria-pressed"), "true");
    assert.match(await results.innerText(), /0 张/);
  }
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await page.getByRole("button", { name: "学习日历", exact: true }).waitFor();
  await page.goBack();
  await page.locator(".calendar-grid").waitFor();
  await page.goForward();
  await page.getByRole("button", { name: "学习日历", exact: true }).waitFor();
  // Visiting the secondary page preserves the current study question and draft.
  await page.getByRole("button", { name: "开始学习", exact: true }).click();
  await page.locator('.question-card[aria-busy="false"]').waitFor();
  await page.locator('.sentence-input[aria-busy="false"]').waitFor();
  await page.locator(".sentence-input").fill("calendar draft");
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await page.getByRole("button", { name: "学习日历", exact: true }).click();
  await page.locator(".calendar-grid").waitFor();
  await page.getByRole("button", { name: "继续学习", exact: true }).click();
  assert.equal(await page.locator(".sentence-input").inputValue(), "calendar draft");
  assert.deepEqual(errors, []);
  console.log("PASS secondary entry, daily totals, month/date selection, browser history, mobile widths, and preserved study draft");
} finally { await browser.close(); }
