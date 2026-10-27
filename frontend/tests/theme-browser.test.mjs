// Start Vite first. All API responses are fixtures; no real account or learning data is used.
// APP_TEST_URL=http://127.0.0.1:5173 BROWSER_PATH=/usr/bin/chromium node tests/theme-browser.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["127.0.0.1", "localhost"].includes(new URL(origin).hostname));
const browser = await chromium.launch({ headless: true, executablePath: process.env.BROWSER_PATH });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true,
    hasTouch: true, colorScheme: "dark", serviceWorkers: "block" });
  let role = null;
  const errors = [];
  const card = { attempt_id: "theme-test", id: 1, sense_id: 1, learning_unit_id: 1, example_id: 1,
    needs_correction: false, pronunciation_used: false, answer_exposed: false, known_candidate: false,
    confirmations: 0, is_relearning: false, answer_form: "apple", is_new_word: true, word: "apple",
    part_of_speech: "n.", definition_cn: "苹果", cloze_sentence: "I ate an _______.",
    example_sentence: "I ate an apple.", status: "New", remaining_today: 1 };
  await context.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let json = {};
    switch (path) {
      case "/api/auth/me":
        if (!role) return route.fulfill({ status: 401, json: { detail: "Sign in" } });
        json = { user: { id: 1, username: "theme-test", role, timezone: "Asia/Shanghai" } }; break;
      case "/api/stats": json = Object.fromEntries(["total_study_time_ms", "today_completed_cards", "due_words",
        "total_learned", "learning", "new_words", "mature", "streak_days", "today_independent_accuracy",
        "lapse_words", "mastered", "learned_senses", "new_senses", "mastered_senses", "legacy_unmapped_words"
      ].map(key => [key, 0])); break;
      case "/api/settings": json = { show_sentence_translation: false, skip_basic_600: false, selected_word_list_ids: [] }; break;
      case "/api/word-lists": json = { lists: [] }; break;
      case "/api/study/meaning-catalog": json = { words: [] }; break;
      case "/api/push/config": json = { enabled: false }; break;
      case "/api/next": json = { card }; break;
      case "/api/admin/overview": json = { users: 1, today_active_users: 0, reviews: 0,
        today_reviews: 0, public_words: 1, pending_reports: 0 }; break;
      case "/api/auth/passkeys/config": json = { enabled: false }; break;
      case "/api/learning-calendar": {
        const today = "2026-10-27";
        json = { month: "2026-10", today, days: Array.from({ length: 31 }, (_, i) => ({
          date: `2026-10-${String(i + 1).padStart(2, "0")}`, reviews: i % 5,
          completed_cards: [0, 5, 15, 40, 70][i % 5], study_time_ms: 0, first_attempt_accuracy: null
        })), summary: { learning_days: 12, completed_cards: 40 } }; break;
      }
    }
    await route.fulfill({ json });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on("pageerror", e => errors.push(e.message));
  const theme = async expected => {
    await page.waitForFunction(value => document.documentElement.dataset.theme === value, expected);
    assert.equal(await page.locator("html").evaluate(el => getComputedStyle(el).colorScheme), expected);
    assert.equal(await page.locator('meta[name="theme-color"]').getAttribute("content"), expected === "dark" ? "#151c19" : "#f7f7f2");
  };
  await page.goto(origin);
  await page.getByRole("group", { name: "外观", exact: true }).waitFor();
  await theme("dark");
  await page.getByRole("button", { name: "浅色", exact: true }).click();
  await theme("light");
  await page.reload();
  await theme("light");
  await page.getByRole("button", { name: "跟随系统", exact: true }).click();
  await theme("dark");
  await page.emulateMedia({ colorScheme: "light" });
  await theme("light");
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await page.emulateMedia({ colorScheme: "light" });
  await theme("dark");
  await page.screenshot({ path: "/tmp/englishlearning-dark-login.png", fullPage: true });

  role = "learner";
  await page.reload();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.locator(".settings-page .theme-options").waitFor();
  assert.equal(await page.getByRole("button", { name: "深色", exact: true }).getAttribute("aria-pressed"), "true");
  for (const width of [320, 390, 430, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `No overflow at ${width}px`);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/englishlearning-dark-settings.png", fullPage: true });
  // Another tab's choice updates both the DOM palette and selected option.
  const other = await context.newPage();
  await other.goto(origin);
  await other.getByRole("button", { name: "设置", exact: true }).click();
  await other.getByRole("button", { name: "浅色", exact: true }).click();
  await theme("light");
  assert.equal(await page.getByRole("button", { name: "浅色", exact: true }).getAttribute("aria-pressed"), "true");
  await page.getByRole("button", { name: "深色", exact: true }).click();
  await other.close();
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await page.screenshot({ path: "/tmp/englishlearning-dark-home.png", fullPage: true });
  await page.getByRole("button", { name: "学习日历", exact: true }).click();
  await page.locator(".calendar-grid").waitFor();
  await page.screenshot({ path: "/tmp/englishlearning-dark-calendar.png", fullPage: true });
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await page.getByRole("button", { name: "开始学习", exact: true }).click();
  const answer = page.getByRole("textbox", { name: "输入英文答案", exact: true });
  await answer.waitFor();
  await answer.fill("draft");
  for (const status of ["is-correct", "is-retry"]) {
    await answer.evaluate((el, status) => { el.className = `sentence-input ${status}`; }, status);
    const contrast = await answer.evaluate(el => {
      const { color, backgroundColor } = getComputedStyle(el);
      const luminance = rgb => {
        const channels = rgb.match(/\d+/g).slice(0, 3).map(Number).map(c => {
          c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
      };
      const levels = [luminance(color), luminance(backgroundColor)].sort((a, b) => b - a);
      return (levels[0] + .05) / (levels[1] + .05);
    });
    assert.ok(contrast >= 4.5, `${status} contrast ${contrast}`);
  }
  await page.screenshot({ path: "/tmp/englishlearning-dark-study.png", fullPage: true });
  assert.equal(await answer.inputValue(), "draft");
  role = "admin";
  await page.goto(`${origin}/#admin`);
  await page.reload();
  await page.locator(".admin-shell .theme-options").waitFor();
  await theme("dark");
  assert.deepEqual(errors, []);
  console.log("PASS login/settings/admin, persistence, system changes, cross-tab sync, mobile widths, calendar, and answer contrast");
} finally { await browser.close(); }
