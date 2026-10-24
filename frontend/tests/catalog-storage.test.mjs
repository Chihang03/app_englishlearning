// Real IndexedDB transactions against a disposable local origin.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import ts from "typescript";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ["localhost", "127.0.0.1"].includes(new URL(origin).hostname));
const source = readFileSync(new URL("../src/studyContentCache.ts", import.meta.url), "utf8")
  .replace('import { ApiError } from "./api";', 'class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 } }).outputText;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(`${origin}/version.json`);
  const result = await page.evaluate(async (source) => {
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    const word = (id, text = `word-${id}`) => ({ id, word: text, senses: [{ id, learning_unit_id: id,
      part_of_speech: "名词", definition_cn: `释义 ${id}`, examples: [{ id, sentence: `A ${text} [测试] 😀.`, target_form: text }] }] });
    const legacyWords = Array.from({ length: 150 }, (_, i) => word(i + 1));
    const legacy = await new Promise((resolve) => {
      const open = indexedDB.open("cvt-study-content-v1", 2);
      open.onupgradeneeded = () => {
        const rich = open.result.createObjectStore("meanings", { keyPath: "key" });
        rich.createIndex("by_account_access", ["accountId", "accessedAt"]);
        open.result.createObjectStore("catalog", { keyPath: "accountId" });
      };
      open.onsuccess = () => resolve(open.result);
    });
    await new Promise((resolve) => {
      const tx = legacy.transaction("catalog", "readwrite");
      tx.objectStore("catalog").put({ accountId: 7, etag: '"legacy"', checkedAt: 0, appVersion: "old", words: legacyWords });
      tx.oncomplete = resolve;
    });
    legacy.close();
    const { StudyContentCache } = await import(`data:text/javascript;base64,${btoa(unescape(encodeURIComponent(source)))}`);
    const cache = new StudyContentCache(7);
    check((await cache.savedWord(100)).word === "word-100", "legacy migration lost content");
    const db = await new Promise((resolve) => { const open = indexedDB.open("cvt-study-content-v1", 3); open.onsuccess = () => resolve(open.result); });
    const get = (store, key) => new Promise((resolve) => { const r = db.transaction(store).objectStore(store).get(key); r.onsuccess = () => resolve(r.result); });
    const count = (store) => new Promise((resolve) => { const r = db.transaction(store).objectStore(store).count(); r.onsuccess = () => resolve(r.result); });
    const meta = await get("catalog-meta", 7);
    check(meta.wordCount === 150 && !meta.words && JSON.stringify(meta).length < 300, "metadata contains dictionary body");
    check(!await get("catalog", 7), "legacy monolith retained after migration");
    const reads = [], writes = [];
    const originalGet = IDBObjectStore.prototype.get, originalPut = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.get = function(...args) { reads.push(this.name); return originalGet.apply(this, args); };
    IDBObjectStore.prototype.put = function(...args) { writes.push(this.name); return originalPut.apply(this, args); };
    window.fetch = async () => new Response(null, { status: 304 });
    const reopened = new StudyContentCache(7);
    await reopened.syncCatalog("new-version", true);
    check(!reads.includes("catalog-words") && !reads.includes("catalog-index"), "startup loaded dictionary body or index");
    check(writes.length === 1 && writes[0] === "catalog-meta", "304 rewrote dictionary contents");
    const matches = await reopened.searchWords("word-1");
    check(matches.length === 50 && matches.every(w => w.word.includes("word-1")), "bounded substring search changed semantics");
    check(!matches[0].senses, "search index contains examples");
    check((await reopened.savedWord(matches[0].id)).senses[0].examples.length === 1, "on-demand content missing");
    const other = new StudyContentCache(8);
    check((await other.searchWords("")).length === 0 && !await other.savedWord(1), "account isolation failed");
    const nextWords = Array.from({ length: 130 }, (_, i) => word(i + 1000));
    const incomplete = JSON.stringify({ words: nextWords }).slice(0, -2);
    window.fetch = async () => new Response(incomplete, { headers: { ETag: '"broken"' } });
    let rejected = false;
    try { await reopened.syncCatalog("broken", true); } catch { rejected = true; }
    check(rejected, "truncated catalogue accepted");
    check((await get("catalog-meta", 7)).generation === meta.generation, "incomplete generation replaced valid cache");
    check(await count("catalog-words") === 150 && await count("catalog-index") === 150, "failed staging left partial rows");
    window.fetch = async () => new Response(JSON.stringify({ words: nextWords }), { headers: { ETag: '"next"' } });
    await reopened.syncCatalog("next", true);
    check(!await reopened.savedWord(1) && (await reopened.savedWord(1000)).word === "word-1000", "generation replacement failed");
    check(await count("catalog-words") === 130 && await count("catalog-index") === 130, "obsolete generation not removed");
    check((await cache.savedWord(1000))?.word === "word-1000", "another instance retained obsolete generation");
    let release304, signalStarted;
    const started = new Promise(resolve => { signalStarted = resolve; });
    window.fetch = () => { signalStarted(); return new Promise(resolve => { release304 = resolve; }); };
    const delayedCheck = cache.syncCatalog("late-304", true);
    await started;
    window.fetch = async () => new Response(JSON.stringify({ words: [word(2000)] }), { headers: { ETag: '"newer"' } });
    await new StudyContentCache(7).syncCatalog("newer", true);
    release304(new Response(null, { status: 304 }));
    await delayedCheck;
    check((await get("catalog-meta", 7)).etag === '"newer"' && (await cache.savedWord(2000))?.id === 2000,
      "a delayed 304 replaced a different tab's published catalogue");
    let resume, signalStaged;
    const staged = new Promise(resolve => { signalStaged = resolve; });
    let pulls = 0;
    window.fetch = async () => new Response(new ReadableStream({ async pull(controller) {
      if (!pulls++) controller.enqueue(new TextEncoder().encode('{"words":[' + Array.from({ length: 65 }, (_, i) => JSON.stringify(word(i + 3000))).join(',')));
      else { signalStaged(); await new Promise(resolve => { resume = resolve; }); controller.enqueue(new TextEncoder().encode(']}')); controller.close(); }
    } }), { headers: { ETag: '"outdated-response"' } });
    const outdated = cache.syncCatalog("outdated", true);
    await staged;
    window.fetch = async () => new Response(JSON.stringify({ words: [word(4000)] }), { headers: { ETag: '"latest"' } });
    await new StudyContentCache(7).syncCatalog("latest", true);
    resume(); await outdated;
    check((await get("catalog-meta", 7)).etag === '"latest"' && (await cache.savedWord(4000))?.id === 4000,
      "an older in-flight download replaced an already published generation");
    await new Promise(resolve => {
      const tx = db.transaction(["catalog-words", "catalog-index"], "readwrite");
      tx.objectStore("catalog-words").put({ ...word(99), accountId: 7, generation: "0-abandoned" });
      tx.objectStore("catalog-index").put({ id: 99, word: "abandoned", search: "abandoned", accountId: 7, generation: "0-abandoned" });
      tx.oncomplete = resolve;
    });
    window.fetch = async () => new Response(null, { status: 304 });
    await cache.syncCatalog("prune", true);
    check(await count("catalog-words") === 1 && await count("catalog-index") === 1, "abandoned download not pruned");
    IDBObjectStore.prototype.get = originalGet; IDBObjectStore.prototype.put = originalPut;
    db.close();
    return { migration: true, metadataBytes: JSON.stringify(meta).length, startupBodyReads: 0,
      notModifiedBodyWrites: 0, boundedSearch: true, accountIsolation: true, interruptedDownloadPreserved: true,
      obsoleteRowsRemoved: true, crossInstanceRefresh: true, concurrent304: true,
      concurrentDownload: true, abandonedDownloadPruned: true };
  }, compiled);
  console.log(JSON.stringify(result));
} finally { await browser.close(); }
