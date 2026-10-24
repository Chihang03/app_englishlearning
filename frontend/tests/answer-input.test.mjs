// Start Vite locally; all API requests are mocked, with no backend or real data.
// APP_TEST_URL=http://127.0.0.1:18765 PLAYWRIGHT_MODULE=/path/to/playwright node tests/answer-input.test.mjs
// Set BROWSER=webkit to run the same cases in WebKit.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const playwright = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const engine = process.env.BROWSER ?? "chromium";
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["127.0.0.1", "localhost"].includes(new URL(origin).hostname), "Use a local test server");
const { version } = JSON.parse(readFileSync(new URL("../src/version.json", import.meta.url), "utf8"));
const card = {
  attempt_id: "input-regression", id: 1, sense_id: 1, learning_unit_id: 1, example_id: 1,
  needs_correction: false, pronunciation_used: false, answer_exposed: false,
  known_candidate: false, confirmations: 0, is_relearning: false,
  answer_form: "apple", is_new_word: true, word: "apple", part_of_speech: "n.",
  definition_cn: "苹果", cloze_sentence: "I ate an _______.", example_sentence: "I ate an apple.",
  status: "New", remaining_today: 1
};
let nextCard = card;
const stats = {
  total_study_time_ms: 0, today_completed_cards: 0, due_words: 0, total_learned: 0,
  learning: 0, new_words: 1, mature: 0, streak_days: 0, today_independent_accuracy: null,
  lapse_words: 0, mastered: 0, learned_senses: 0, new_senses: 1, mastered_senses: 0,
  legacy_unmapped_words: 0, next_relearning_at: null
};
const browser = await playwright[engine].launch({ headless: true, ...(engine === "chromium" ? { channel: "chrome" } : {}) });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, serviceWorkers: "block" });
  await context.addInitScript(() => {
    window.testSpeech = [];
    const synth = new EventTarget();
    synth.getVoices = () => [];
    synth.cancel = () => {};
    synth.speak = (utterance) => {
      window.testSpeech.push(utterance.text);
      utterance.onstart?.(new Event("start"));
      // Do not end speech: keep the accepted card visible for duplicate checks.
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  const reviews = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let releaseReview;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let json;
    switch (path) {
      case "/api/auth/me": json = { user: { id: 1, username: "input-test", role: "learner", timezone: "Asia/Shanghai" } }; break;
      case "/api/version": json = { version }; break;
      case "/api/stats": json = stats; break;
      case "/api/settings": json = { show_sentence_translation: false, skip_basic_600: false, selected_word_list_ids: [], speech_rate: 120 }; break;
      case "/api/word-lists": json = { lists: [] }; break;
      case "/api/study/meaning-catalog": json = { words: [] }; break;
      case "/api/push/config": json = { enabled: false, public_key: null }; break;
      case "/api/next": json = { card: nextCard }; break;
      case "/api/review": {
        const body = route.request().postDataJSON();
        reviews.push(body);
        if (releaseReview) await new Promise((resolve) => { releaseReview.resolve = resolve; });
        const correct = body.user_answer.trim() === nextCard.answer_form;
        json = { sense_id: 1, learning_unit_id: 1, example_id: 1, word: "apple", other_senses: [],
          is_correct: correct, is_independent: false, outcome: correct ? "corrected" : "incorrect",
          is_blank: !body.user_answer.trim(), correct_answer: nextCard.answer_form, example_sentence: nextCard.example_sentence };
        break;
      }
      default:
        errors.push(`Unexpected API request: ${path}`);
        return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
    }
    await route.fulfill({ json });
  });
  const answer = page.getByRole("textbox", { name: "输入英文答案", exact: true });
  const waitForReview = () => page.waitForResponse((response) => new URL(response.url()).pathname === "/api/review");
  const openCard = async (fixture = card) => {
    nextCard = fixture;
    reviews.length = 0;
    await page.goto(origin);
    await page.getByRole("button", { name: "开始学习", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#study-answer")?.getAttribute("aria-busy") === "false");
  };
  const wrongAnswer = async (fixture = card) => {
    await openCard(fixture);
    await answer.fill("wrong");
    const response = waitForReview();
    await answer.press("Enter");
    await response;
    await page.waitForFunction((expected) => {
      const field = document.querySelector("#study-answer");
      return field?.value === "" && field.placeholder === expected
        && field.getAttribute("aria-busy") === "false";
    }, fixture.answer_form);
    assert.equal(reviews.length, 1);
  };
  // Model a keyboard optimizing away identical replacements, including both
  // beforeinput and input. The correction hint must not occupy the edit buffer.
  const replace = async (value, inputType = "insertReplacementText") => answer.evaluate((field, edit) => {
    const next = field.value.slice(0, field.selectionStart) + edit.value + field.value.slice(field.selectionEnd);
    if (next === field.value) return;
    const before = new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: edit.inputType, data: edit.value });
    if (!field.dispatchEvent(before)) return;
    field.setRangeText(edit.value, field.selectionStart, field.selectionEnd, "end");
    field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: edit.inputType, data: edit.value }));
  }, { value, inputType });
  const submit = async (value) => {
    const response = waitForReview();
    await answer.press("Enter");
    await response;
    assert.equal(reviews.length, 2, "One retry submission");
    assert.equal(reviews[1].user_answer, value);
    assert.equal(reviews[1].attempt_id, card.attempt_id);
  };

  for (const inputType of ["insertReplacementText", "insertText"]) {
    await wrongAnswer();
    await replace("apple", inputType);
    assert.equal(await answer.inputValue(), "apple", "Same-word replacement must retain the answer");
    await submit("apple");
    await answer.press("Enter");
    assert.equal(reviews.length, 2, "Accepted feedback must not resubmit");
    console.log(`PASS ${engine}: identical ${inputType} replacement retries once with the latest answer`);
  }

  await wrongAnswer();
  await answer.focus();
  await page.keyboard.insertText("apple");
  await submit("apple");
  console.log(`PASS ${engine}: browser-native text insertion retries the displayed correct word`);

  await wrongAnswer();
  await answer.click();
  await answer.evaluate((field) => { field.select(); });
  const spoken = await page.evaluate(() => window.testSpeech.length);
  await answer.press("Enter");
  assert.equal(reviews.length, 1, "Focus/selection alone must not start a retry");
  assert.equal(await page.evaluate(() => window.testSpeech.length), spoken + 1, "Unedited feedback replays speech");
  console.log(`PASS ${engine}: click/selection without editing retains feedback and replay`);

  await wrongAnswer();
  await answer.press("a");
  await answer.pressSequentially("pple");
  await submit("apple");
  console.log(`PASS ${engine}: ordinary character-by-character retry`);

  await wrongAnswer();
  await replace("pear");
  await submit("pear");
  await page.waitForFunction(() => {
    const field = document.querySelector("#study-answer");
    return field?.value === "" && field.placeholder === "apple";
  });
  console.log(`PASS ${engine}: different replacement submits the actual text and displays feedback`);

  await wrongAnswer();
  await page.keyboard.insertText("pear");
  await answer.evaluate((field) => field.select());
  await answer.press("Backspace");
  assert.equal(await answer.inputValue(), "");
  await answer.pressSequentially("apple");
  await submit("apple");
  console.log(`PASS ${engine}: deletion followed by retyping`);

  await wrongAnswer();
  // During composition even an unchanged value must not turn Enter into submit.
  await answer.dispatchEvent("compositionstart", { data: "" });
  await replace("apple", "insertCompositionText");
  // A real IME consumes this key to confirm a candidate instead of inserting
  // a textarea newline. Dispatch only its key event to model that behavior.
  await answer.dispatchEvent("keydown", { key: "Enter", code: "Enter", keyCode: 229, isComposing: true });
  assert.equal(reviews.length, 1);
  await answer.dispatchEvent("compositionend", { data: "apple" });
  await submit("apple");
  console.log(`PASS ${engine}: composition Enter is ignored; committed same-word retry submits`);

  await wrongAnswer();
  await replace("apple");
  releaseReview = {};
  const submitted = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/review");
  await answer.press("Enter");
  await submitted;
  await page.waitForFunction(() => document.querySelector("#study-answer")?.getAttribute("aria-busy") === "true");
  await answer.press("Enter");
  await replace("ignored");
  assert.equal(reviews.length, 2, "Pending submission must reject duplicate Enter");
  const completed = waitForReview();
  releaseReview.resolve();
  releaseReview = undefined;
  await completed;
  await page.waitForFunction(() => document.querySelector("#study-answer")?.classList.contains("is-correct"));
  assert.equal(await answer.inputValue(), "apple");
  assert.equal(reviews[1].user_answer, "apple");
  console.log(`PASS ${engine}: pending retry ignores duplicate submission and edits`);

  await openCard({ ...card, needs_correction: true });
  assert.equal(await answer.inputValue(), "", "Resumed correction must also start with an empty edit buffer");
  assert.equal(await answer.getAttribute("placeholder"), "apple");
  await page.keyboard.insertText("apple");
  const resumed = waitForReview();
  await answer.press("Enter");
  await resumed;
  assert.equal(reviews.length, 1);
  assert.equal(reviews[0].user_answer, "apple");
  console.log(`PASS ${engine}: resumed correction displays the answer without prefilling it`);

  const longWord = "pneumonoultramicroscopicsilicovolcanoconiosis";
  await page.setViewportSize({ width: 320, height: 844 });
  await wrongAnswer({ ...card, answer_form: longWord, example_sentence: `The word is ${longWord}.`, cloze_sentence: "The word is _______." });
  const layout = await answer.evaluate((field) => {
    const style = getComputedStyle(field);
    const hint = getComputedStyle(field, "::placeholder");
    return { height: field.clientHeight, scrollHeight: field.scrollHeight, lineHeight: parseFloat(style.lineHeight),
      color: style.color, hintColor: hint.color, hintOpacity: hint.opacity };
  });
  assert.ok(layout.height >= layout.lineHeight * 2, "Long correction hint must wrap without clipping");
  assert.ok(layout.scrollHeight <= layout.height + 1, "Correction hint must fit the input height");
  assert.equal(layout.hintColor, layout.color);
  assert.equal(layout.hintOpacity, "1");
  await page.screenshot({ path: process.env.INPUT_TEST_SCREENSHOT ?? "/tmp/english-answer-correction.png" });
  await page.keyboard.insertText(longWord);
  await submit(longWord);
  console.log(`PASS ${engine}: narrow-screen correction retains full color and wraps long words`);
  assert.deepEqual(errors, [], "No browser errors or unmocked API requests");
  await context.close();
} finally {
  await browser.close();
}
