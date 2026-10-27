import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/theme.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS
} }).outputText;
const bootstrap = readFileSync(new URL("../index.html", import.meta.url), "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];

function environment({ saved = null, dark = false, blocked = false } = {}) {
  const mediaListeners = [];
  const events = new Map();
  const media = { matches: dark, addEventListener: (_, listener) => mediaListeners.push(listener) };
  const meta = { content: "", setAttribute: (_, value) => { meta.content = value; } };
  const document = { documentElement: { dataset: {} }, querySelector: () => meta };
  const localStorage = {
    getItem: () => { if (blocked) throw new Error("Storage blocked"); return saved; },
    setItem: (_, value) => { if (blocked) throw new Error("Storage blocked"); saved = value; }
  };
  const matchMedia = () => media;
  const window = { matchMedia, addEventListener: (event, listener) => events.set(event, listener) };
  const context = vm.createContext({ exports: {}, document, window, localStorage, matchMedia });
  vm.runInContext(compiled, context);
  return { api: context.exports, document, meta,
    bootstrap: () => vm.runInContext(bootstrap, context),
    system: (dark) => { media.matches = dark; mediaListeners.forEach(listener => listener()); },
    storage: (key, newValue) => events.get("storage")({ key, newValue }),
    saved: () => saved,
    theme: () => document.documentElement.dataset.theme };
}

test("first-paint theme and runtime agree for system, explicit, invalid and unavailable storage", () => {
  for (const dark of [false, true]) {
    for (const saved of [null, "system", "light", "dark", "invalid"]) {
      for (const blocked of [false, true]) {
        const env = environment({ saved, dark, blocked });
        env.bootstrap();
        const initial = env.theme();
        env.api.initializeTheme();
        assert.equal(env.theme(), initial);
        assert.equal(env.theme(), (!blocked && saved === "dark") ||
          (dark && (blocked || saved !== "light")) ? "dark" : "light");
      }
    }
  }
});

test("system follows live changes; explicit choices override them and update browser chrome", () => {
  const env = environment();
  env.api.initializeTheme();
  assert.equal(env.api.getThemePreference(), "system");
  env.system(true);
  assert.equal(env.theme(), "dark");
  assert.equal(env.meta.content, "#151c19");
  env.api.setThemePreference("light");
  env.system(true);
  assert.equal(env.theme(), "light");
  assert.equal(env.saved(), "light");
  assert.equal(env.meta.content, "#f7f7f2");
  env.api.setThemePreference("dark");
  env.system(false);
  assert.equal(env.theme(), "dark");
  env.api.setThemePreference("system");
  assert.equal(env.theme(), "light");
});

test("cross-tab changes and clearing storage update subscribers; unrelated keys do not", () => {
  const env = environment({ dark: true });
  env.api.initializeTheme();
  let calls = 0;
  const unsubscribe = env.api.subscribeTheme(() => calls++);
  env.storage("other-setting", "light");
  assert.equal(calls, 0);
  env.storage("context-theme", "light");
  assert.equal(env.theme(), "light");
  assert.equal(env.api.getThemePreference(), "light");
  env.storage(null, null);
  assert.equal(env.theme(), "dark");
  assert.equal(env.api.getThemePreference(), "system");
  assert.equal(calls, 2);
  unsubscribe();
  env.api.setThemePreference("dark");
  assert.equal(calls, 2);
});

test("storage failures retain the selected theme for this session", () => {
  const env = environment({ blocked: true });
  env.api.initializeTheme();
  env.api.setThemePreference("dark");
  assert.equal(env.theme(), "dark");
  assert.equal(env.api.getThemePreference(), "dark");
  env.system(false);
  assert.equal(env.theme(), "dark");
});
