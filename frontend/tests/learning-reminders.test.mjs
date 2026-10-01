// APP_TEST_URL=http://127.0.0.1:18765 PLAYWRIGHT_MODULE=/path/to/playwright node tests/learning-reminders.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { randomBytes, createECDH } from "node:crypto";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["localhost", "127.0.0.1"].includes(new URL(origin).hostname), "Use a disposable local server");
const ecdh = createECDH("prime256v1"); ecdh.generateKeys();
const subscription = { endpoint: `https://web.push.apple.com/local-ui-test-${randomBytes(8).toString("hex")}`, keys: {
  p256dh: ecdh.getPublicKey().toString("base64url"), auth: randomBytes(16).toString("base64url")
} };

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  for (const scenario of ["allow", "deny", "granted", "ios-tab"]) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await context.addInitScript(({ scenario, subscription }) => {
      const state = { permission: scenario === "granted" ? "granted" : "default", asks: 0, subscribed: scenario === "granted", gestures: [] };
      window.__pushTest = state;
      Object.defineProperty(Notification, "permission", { get: () => state.permission });
      Object.defineProperty(Notification, "requestPermission", { value: async () => {
        state.asks++; state.gestures.push(navigator.userActivation.isActive);
        state.permission = scenario === "deny" ? "denied" : "granted";
        return state.permission;
      } });
      const getSubscription = () => ({ toJSON: () => subscription });
      PushManager.prototype.getSubscription = async () => state.subscribed ? getSubscription() : null;
      PushManager.prototype.subscribe = async () => { state.subscribed = true; return getSubscription(); };
      Object.defineProperty(window.speechSynthesis, "getVoices", { value: () => [] });
      if (scenario === "ios-tab") Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X)" });
    }, { scenario, subscription: { ...subscription, endpoint: `${subscription.endpoint}-${scenario}` } });
    const account = await context.request.post(`${origin}/api/auth/register`, { data: {
      username: `push-${scenario}-${Date.now()}`, password: "local-test-password", timezone: "Asia/Shanghai"
    } });
    assert.equal(account.status(), 200);
    const page = await context.newPage();
    const errors = [];
    const writes = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => { if (request.url().endsWith("/api/push/subscription")) writes.push(request); });
    const configResponse = scenario === "ios-tab" ? null
      : page.waitForResponse((response) => response.url().endsWith("/api/push/config"));
    await page.goto(origin);
    await page.getByRole("button", { name: "开始学习", exact: true }).waitFor();
    if (scenario !== "ios-tab") {
      await page.waitForFunction(() => navigator.serviceWorker.controller);
      await configResponse;
    }
    assert.equal(await page.getByRole("button", { name: "开启学习提醒" }).count(), 0);
    assert.equal(await page.evaluate(() => window.__pushTest.asks), 0, "No startup permission prompt");
    const subscriptionSaved = scenario === "allow" ? page.waitForResponse((response) => response.url().endsWith("/api/push/subscription") && response.status() === 200) : null;
    await page.getByRole("button", { name: "开始学习", exact: true }).click();
    await page.getByRole("button", { name: "返回首页", exact: true }).waitFor();
    if (scenario === "allow") {
      await subscriptionSaved;
      assert.deepEqual(await page.evaluate(() => window.__pushTest.gestures), [true]);
    }
    await page.getByRole("button", { name: "返回首页", exact: true }).click();
    await page.getByRole("button", { name: "继续学习", exact: true }).click();
    assert.equal(await page.evaluate(() => window.__pushTest.asks), ["allow", "deny"].includes(scenario) ? 1 : 0);
    if (["deny", "ios-tab"].includes(scenario)) assert.equal(writes.length, 0);
    if (scenario === "granted") assert.ok(writes.length > 0, "Granted permission reconnects the subscription");
    if (scenario === "allow") {
      await page.goto(`${origin}/?notification=study#study`);
      await page.getByRole("button", { name: "返回首页", exact: true }).waitFor();
      assert.equal(new URL(page.url()).search, "");
      assert.equal(new URL(page.url()).hash, "#study");
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`PASS ${scenario}`);
  }
} finally { await browser.close(); }
