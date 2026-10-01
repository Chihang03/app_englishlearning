import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Exercise the actual TypeScript module without adding a second bundler/runtime.
async function loadSource(name) {
  let source = readFileSync(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  if (name === "studyContentCache") source = source.replace('import { ApiError } from "./api";',
    "const ApiError = globalThis.__cacheTestApiError;");
  const { outputText } = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020
  } });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
const { ReadCache } = await loadSource("readCache");
const api = await loadSource("api");
globalThis.__cacheTestApiError = api.ApiError;
const { StudyContentCache } = await loadSource("studyContentCache");
delete globalThis.__cacheTestApiError;
const { watchForeground, canReloadForUpdate } = await loadSource("foreground");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("reuses fresh data, deduplicates concurrent reads, and supports explicit refresh", async () => {
  let calls = 0;
  const gate = deferred();
  const cache = new ReadCache(() => { calls++; return calls === 1 ? gate.promise : Promise.resolve(calls); });
  const first = cache.get("/stats", 10000);
  const second = cache.get("/stats", 10000);
  assert.equal(calls, 1);
  gate.resolve(1);
  assert.deepEqual(await Promise.all([first, second]), [1, 1]);
  assert.equal(await cache.get("/stats", 10000), 1);
  assert.equal(calls, 1);
  assert.equal(await cache.get("/stats", 10000, true), 2);
});

test("an in-flight read invalidated by a write cannot return or store stale data", async () => {
  let calls = 0;
  const old = deferred();
  const cache = new ReadCache(() => ++calls === 1 ? old.promise : Promise.resolve("after write"));
  const pending = cache.get("/settings", 10000);
  cache.clear();
  const fresh = cache.get("/settings", 10000);
  old.resolve("before write");
  assert.deepEqual(await Promise.all([pending, fresh]), ["after write", "after write"]);
  assert.equal(await cache.get("/settings", 10000), "after write");
  assert.equal(calls, 2);
});

test("expired data and failed requests are retried", async () => {
  let calls = 0;
  const cache = new ReadCache(async () => {
    if (++calls === 1) throw new Error("network unavailable");
    return calls;
  });
  await assert.rejects(cache.get("/stats", 0), /network unavailable/);
  assert.equal(await cache.get("/stats", 0), 2);
  assert.equal(await cache.get("/stats", 0), 3);
});

test("separate account caches never reuse another account's response", async () => {
  const first = new ReadCache(async () => "learner A");
  const second = new ReadCache(async () => "learner B");
  assert.equal(await first.get("/stats", 10000), "learner A");
  assert.equal(await second.get("/stats", 10000), "learner B");
  first.clear();
});

test("study content keeps the fifty most recent word views and expires old entries", async () => {
  const originalNow = Date.now;
  let now = 1000;
  Date.now = () => now;
  try {
    const cache = new StudyContentCache(7, 50, 10000);
    for (let id = 1; id <= 50; id++) await cache.set(id, null, { word: String(id) });
    assert.equal((await cache.get(1, null)).content.word, "1");
    await cache.set(51, null, { word: "51" });
    assert.equal(await cache.get(2, null), undefined);
    assert.equal(await cache.get(1, 99), undefined);
    assert.equal((await cache.get(1, null)).content.word, "1");
    now += 10000;
    assert.equal(await cache.get(1, null), undefined);
    await cache.set(1, 99, { word: "related" });
    assert.equal((await cache.get(1, 99)).content.word, "related");
    await cache.clear();
    assert.equal(await cache.get(1, 99), undefined);
  } finally { Date.now = originalNow; }
});

test("app version changes compare dictionary content and preserve unchanged local meanings", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const words = (definition) => [{ id: 7, word: "bank", senses: [{ id: 8, learning_unit_id: 9,
    part_of_speech: "名词", definition_cn: definition, definition_en: null, examples: [] }] }];
  let revision = 1;
  globalThis.fetch = async (_path, options) => {
    requests.push(options.headers["If-None-Match"] ?? null);
    if (options.headers["If-None-Match"] === `"${revision}"`) {
      return new Response(null, { status: 304, headers: { ETag: `"${revision}"` } });
    }
    return new Response(JSON.stringify({ words: words(`meaning ${revision}`) }),
      { headers: { ETag: `"${revision}"` } });
  };
  try {
    const cache = new StudyContentCache(7);
    await cache.syncCatalog("v1");
    assert.equal((await cache.get(7, null)).content.senses[0].definition_cn, "meaning 1");
    await cache.set(7, null, { word: "bank", senses: [{ definition_cn: "rich 1" }] });
    await cache.syncCatalog("v2");
    assert.deepEqual(requests, [null, '"1"']);
    assert.equal((await cache.get(7, null)).content.senses[0].definition_cn, "rich 1");
    revision = 2;
    await cache.syncCatalog("v3");
    assert.deepEqual(requests, [null, '"1"', '"1"']);
    assert.equal((await cache.get(7, null)).content.senses[0].definition_cn, "meaning 2");
  } finally { globalThis.fetch = originalFetch; }
});

test("foreground refresh covers long background stays, history restore and reconnection", () => {
  const originalNow = Date.now;
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  let now = 100000;
  Date.now = () => now;
  globalThis.document = Object.assign(new EventTarget(), { hidden: false });
  globalThis.window = new EventTarget();
  let calls = 0;
  const stop = watchForeground(() => calls++);
  const visibility = (hidden) => {
    document.hidden = hidden;
    document.dispatchEvent(new Event("visibilitychange"));
  };
  try {
    visibility(true); now += 1000; visibility(false);
    assert.equal(calls, 0);
    visibility(true); now += 30000; visibility(false);
    assert.equal(calls, 1);
    window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true }));
    window.dispatchEvent(new Event("online"));
    assert.equal(calls, 3);
    visibility(true);
    window.dispatchEvent(new Event("online"));
    assert.equal(calls, 3);
    stop();
    now += 60000; visibility(false);
    window.dispatchEvent(new Event("online"));
    assert.equal(calls, 3);
  } finally {
    stop(); Date.now = originalNow;
    globalThis.document = originalDocument;
    globalThis.window = originalWindow;
  }
});

test("version update never reloads a study/settings page, draft, pending operation or offline page", () => {
  const originalDocument = globalThis.document;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  globalThis.document = { hidden: false };
  Object.defineProperty(globalThis, "navigator", { value: { onLine: true }, configurable: true });
  const safe = { page: "home", draft: false, busy: false };
  try {
    assert.equal(canReloadForUpdate(safe), true);
    for (const changed of [{ page: "study" }, { page: "settings" }, { draft: true }, { busy: true }]) {
      assert.equal(canReloadForUpdate({ ...safe, ...changed }), false);
    }
    document.hidden = true;
    assert.equal(canReloadForUpdate(safe), false);
    document.hidden = false; navigator.onLine = false;
    assert.equal(canReloadForUpdate(safe), false);
  } finally {
    globalThis.document = originalDocument;
    Object.defineProperty(globalThis, "navigator", originalNavigator);
  }
});

test("API bypasses HTTP cache and invalidates on both successful and lost write responses", async () => {
  const originalFetch = globalThis.fetch;
  let writes = 0;
  const unsubscribe = api.onApiWrite(() => writes++);
  try {
    globalThis.fetch = async (_path, options) => {
      assert.equal(options.cache, "no-store");
      assert.equal(options.credentials, "include");
      assert.equal(api.hasPendingWrites(), options.method === "PATCH");
      return new Response(JSON.stringify({ value: 1 }));
    };
    await api.request("/api/settings");
    assert.equal(writes, 0);
    await api.request("/api/settings", { method: "PATCH" });
    assert.equal(writes, 1);
    globalThis.fetch = async () => { throw new TypeError("lost response"); };
    await assert.rejects(api.request("/api/settings", { method: "PATCH" }), /lost response/);
    assert.equal(writes, 2);
    assert.equal(api.hasPendingWrites(), false);
  } finally { unsubscribe(); globalThis.fetch = originalFetch; }
});
