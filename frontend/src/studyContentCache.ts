import type { StudyDetails } from "./types";
import { ApiError } from "./api";

type Entry = {
  key: string;
  accountId: number;
  content: StudyDetails;
  expires: number;
  accessedAt: number;
};
export type CatalogWord = { id: number; word: string; senses: StudyDetails["senses"] };
export type CatalogSummary = { id: number; word: string; definition_cn: string; search: string };
type Catalog = { accountId: number; etag: string; checkedAt: number; appVersion: string; generation: string; wordCount: number };
type LegacyCatalog = Omit<Catalog, "generation" | "wordCount"> & { words: CatalogWord[] };
export type CachedMeanings = { content: StudyDetails; complete: boolean };

const STORE = "meanings";
const CATALOG_STORE = "catalog-meta";
const WORD_STORE = "catalog-words";
const INDEX_STORE = "catalog-index";
const LEGACY_STORE = "catalog";
const wordSizes = new WeakMap<CatalogWord, number>();
let databasePromise: Promise<IDBDatabase | null> | undefined;
let persistenceRequested = false;

function database(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  if (!databasePromise) databasePromise = new Promise((resolve) => {
    try {
      const request = indexedDB.open("cvt-study-content-v1", 3);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) {
          const store = request.result.createObjectStore(STORE, { keyPath: "key" });
          store.createIndex("by_account_access", ["accountId", "accessedAt"]);
        }
        for (const name of [WORD_STORE, INDEX_STORE]) {
          if (!request.result.objectStoreNames.contains(name)) {
            request.result.createObjectStore(name, { keyPath: ["accountId", "generation", "id"] });
          }
        }
        if (!request.result.objectStoreNames.contains(CATALOG_STORE)) {
          request.result.createObjectStore(CATALOG_STORE, { keyPath: "accountId" });
        }
      };
      request.onsuccess = () => {
        request.result.onversionchange = () => { request.result.close(); databasePromise = undefined; };
        databasePromise = Promise.resolve(request.result);
        resolve(request.result);
      };
      request.onerror = request.onblocked = () => { databasePromise = undefined; resolve(null); };
    } catch { resolve(null); }
  });
  return databasePromise;
}

// Parse the existing JSON contract one word at a time. Retained text is bounded
// by one word and one network chunk, rather than the complete dictionary.
export async function* streamCatalogWords(response: Response): AsyncGenerator<CatalogWord> {
  if (!response.body) throw new Error("词义目录读取失败");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let prefix = "", tail = "", started = false, ended = false;
  let depth = 0, quoted = false, escaped = false, expectWord = true, hadWord = false;
  let parts: string[] = [], size = 0, total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      const chunk = decoder.decode(value, { stream: !done });
      total += value?.byteLength ?? 0;
      if (total > 256 * 1024 * 1024) throw new Error("词义目录过大");
      let segment = depth ? 0 : -1;
      for (let i = 0; i < chunk.length; i++) {
        const ch = chunk[i];
        if (!started) {
          prefix += ch;
          if (prefix.length > 64) throw new Error("词义目录格式错误");
          if (ch === "[") {
            if (!/^\s*\{\s*"words"\s*:\s*\[$/.test(prefix)) throw new Error("词义目录格式错误");
            started = true;
          }
          continue;
        }
        if (ended) {
          tail += ch;
          if (tail.length > 64) throw new Error("词义目录格式错误");
          continue;
        }
        if (!depth) {
          if (/\s/.test(ch)) continue;
          if (ch === "]" && (!expectWord || !hadWord)) { ended = true; continue; }
          if (ch === "," && !expectWord) { expectWord = true; continue; }
          if (ch !== "{" || !expectWord) throw new Error("词义目录格式错误");
          depth = 1; segment = i; quoted = false; escaped = false;
          continue;
        }
        if (quoted) {
          if (escaped) escaped = false;
          else if (ch === "\\") escaped = true;
          else if (ch === '"') quoted = false;
        } else if (ch === '"') quoted = true;
        else if (ch === "{" || ch === "[") depth++;
        else if (ch === "}" || ch === "]") depth--;
        if (depth === 0) {
          parts.push(chunk.slice(segment, i + 1));
          size += i + 1 - segment;
          if (size > 2 * 1024 * 1024) throw new Error("词条过大");
          const word = JSON.parse(parts.join("")) as CatalogWord;
          if (!Number.isSafeInteger(word.id) || word.id <= 0 || typeof word.word !== "string" || !Array.isArray(word.senses)) {
            throw new Error("词义目录格式错误");
          }
          wordSizes.set(word, size);
          parts = []; size = 0; segment = -1; expectWord = false; hadWord = true;
          yield word;
        }
      }
      if (depth && segment >= 0) { parts.push(chunk.slice(segment)); size += chunk.length - segment; }
      if (size > 2 * 1024 * 1024) throw new Error("词条过大");
      if (done) break;
    }
    if (!started || !ended || depth || !/^\s*}\s*$/.test(tail)) throw new Error("词义目录不完整");
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

function generationRange(accountId: number, generation: string) {
  return IDBKeyRange.bound([accountId, generation, 0], [accountId, generation, Number.MAX_SAFE_INTEGER]);
}

function readRecord<T>(db: IDBDatabase, store: string, key: IDBValidKey): Promise<T | undefined> {
  return new Promise((resolve) => {
    try {
      const request = db.transaction(store).objectStore(store).get(key);
      request.onsuccess = () => resolve(request.result as T | undefined);
      request.onerror = () => resolve(undefined);
    } catch { resolve(undefined); }
  });
}

// Display data only. Exposure uploads are kept separately and block grading.
export class StudyContentCache {
  private entries = new Map<string, Entry>();
  private catalog: Catalog | null = null;
  private fallbackWords = new Map<number, CatalogWord>();
  private searchIndex?: { generation: string; words: CatalogSummary[] };
  private catalogLoaded?: Promise<void>;
  private catalogSync?: Promise<void>;

  constructor(private accountId: number, private limit = 50, private ttl = 30 * 24 * 60 * 60000) {}

  private key(wordId: number, targetUnitId: number | null) {
    return `${this.accountId}:${wordId}:${targetUnitId ?? "main"}`;
  }

  private range() {
    return IDBKeyRange.bound([this.accountId, 0], [this.accountId, Number.MAX_SAFE_INTEGER]);
  }

  private remember(entry: Entry) {
    this.entries.delete(entry.key);
    this.entries.set(entry.key, entry);
    if (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value!);
  }

  private loadCatalog(): Promise<void> {
    if (!this.catalogLoaded) this.catalogLoaded = (async () => {
      const db = await database();
      if (!db) { this.catalogLoaded = undefined; return; }
      this.catalog = await readRecord<Catalog>(db, CATALOG_STORE, this.accountId) ?? null;
      if (!this.catalog && db.objectStoreNames.contains(LEGACY_STORE)) {
        const legacy = await readRecord<LegacyCatalog>(db, LEGACY_STORE, this.accountId);
        if (legacy?.words) {
          const metadata = await this.storeCatalog((async function* () { yield* legacy.words; })(), legacy);
          this.catalog = metadata;
          // Delete the old monolith only after the complete replacement commits.
          await new Promise<void>((resolve) => {
            const tx = db.transaction(LEGACY_STORE, "readwrite");
            tx.objectStore(LEGACY_STORE).delete(this.accountId);
            tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
          });
        }
      }
    })().catch((error) => { this.catalogLoaded = undefined; throw error; });
    return this.catalogLoaded;
  }

  private async removeGeneration(db: IDBDatabase, generation: string) {
    await new Promise<void>((resolve) => {
      const tx = db.transaction([CATALOG_STORE, WORD_STORE, INDEX_STORE], "readwrite");
      const check = tx.objectStore(CATALOG_STORE).get(this.accountId);
      check.onsuccess = () => {
        if (check.result?.generation === generation) return;
        for (const name of [WORD_STORE, INDEX_STORE]) tx.objectStore(name).delete(generationRange(this.accountId, generation));
      };
      tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
    });
  }

  private async pruneInterruptedDownloads(db: IDBDatabase) {
    await new Promise<void>((resolve) => {
      const tx = db.transaction([CATALOG_STORE, WORD_STORE, INDEX_STORE], "readwrite");
      const current = tx.objectStore(CATALOG_STORE).get(this.accountId);
      current.onsuccess = () => {
        const request = tx.objectStore(WORD_STORE).openKeyCursor(
          IDBKeyRange.bound([this.accountId, ""], [this.accountId, "\uffff"]));
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) return;
          const generation = (cursor.key as [number, string, number])[1];
          if (generation !== current.result?.generation && Number(generation.split("-")[0]) < Date.now() - 86400000) {
            for (const name of [WORD_STORE, INDEX_STORE]) tx.objectStore(name).delete(generationRange(this.accountId, generation));
          }
          // Visit one key per generation, without loading or scanning its words.
          cursor.continue([this.accountId, `${generation}\0`, 0]);
        };
      };
      tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
    });
  }

  private async storeCatalog(words: AsyncIterable<CatalogWord>, metadata: Pick<Catalog, "etag" | "checkedAt" | "appVersion">): Promise<Catalog> {
    const db = await database();
    const generation = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const previous = this.catalog?.generation;
    const fallback = new Map<number, CatalogWord>();
    let wordCount = 0, lastId = 0, batchChars = 0, batch: CatalogWord[] = [];
    const writeBatch = async () => {
      if (!db) { batch = []; batchChars = 0; return; }
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction([WORD_STORE, INDEX_STORE], "readwrite");
        for (const word of batch) {
          tx.objectStore(WORD_STORE).put({ accountId: this.accountId, generation,
            id: word.id, word: word.word, senses: word.senses });
          tx.objectStore(INDEX_STORE).put({ accountId: this.accountId, generation, id: word.id,
            word: word.word, search: word.word.toLowerCase(), definition_cn: word.senses[0]?.definition_cn ?? "" });
        }
        tx.oncomplete = () => resolve();
        tx.onerror = tx.onabort = () => reject(tx.error ?? new Error("词义目录保存失败"));
      });
      batch = []; batchChars = 0;
    };
    try {
      for await (const word of words) {
        if (word.id <= lastId) throw new Error("词义目录顺序错误");
        lastId = word.id; wordCount++;
        const chars = wordSizes.get(word) ?? JSON.stringify(word).length;
        if (batch.length && batchChars + chars > 256 * 1024) await writeBatch();
        if (!db) { fallback.set(word.id, word); if (fallback.size > this.limit) fallback.delete(fallback.keys().next().value!); }
        batch.push(word);
        batchChars += chars;
        if (batch.length >= 64 || batchChars >= 256 * 1024) await writeBatch();
      }
      await writeBatch();
      const catalog: Catalog = { accountId: this.accountId, etag: metadata.etag,
        checkedAt: metadata.checkedAt, appVersion: metadata.appVersion, generation, wordCount };
      let published = catalog;
      if (db) await new Promise<void>((resolve, reject) => {
        const tx = db.transaction([CATALOG_STORE, STORE], "readwrite");
        const current = tx.objectStore(CATALOG_STORE).get(this.accountId);
        current.onsuccess = () => {
          // Another tab may publish while this download is in flight.
          if (current.result && current.result.generation !== previous) {
            published = current.result as Catalog;
            return;
          }
          tx.objectStore(CATALOG_STORE).put(catalog);
          tx.objectStore(STORE).index("by_account_access").openCursor(this.range()).onsuccess = (event) => {
            const cursor = (event.target as IDBRequest<IDBCursorWithValue | null>).result;
            if (cursor) { cursor.delete(); cursor.continue(); }
          };
        };
        tx.oncomplete = () => resolve();
        tx.onerror = tx.onabort = () => reject(tx.error ?? new Error("词义目录保存失败"));
      });
      this.entries.clear(); this.searchIndex = undefined; this.fallbackWords = fallback;
      if (db && published !== catalog) await this.removeGeneration(db, generation);
      else if (db && previous) await this.removeGeneration(db, previous);
      return published;
    } catch (error) {
      if (db) await this.removeGeneration(db, generation);
      throw error;
    }
  }

  async savedWord(wordId: number): Promise<CatalogWord | undefined> {
    await this.loadCatalog();
    const db = await database();
    if (db) this.catalog = await readRecord<Catalog>(db, CATALOG_STORE, this.accountId) ?? this.catalog;
    if (!this.catalog) return undefined;
    if (!db) return this.fallbackWords.get(wordId);
    return readRecord<CatalogWord>(db, WORD_STORE, [this.accountId, this.catalog.generation, wordId]);
  }

  async searchWords(query: string, limit = 50): Promise<CatalogSummary[]> {
    if (limit <= 0) return [];
    await this.loadCatalog();
    const db = await database();
    if (db) this.catalog = await readRecord<Catalog>(db, CATALOG_STORE, this.accountId) ?? this.catalog;
    if (!this.catalog) return [];
    if (this.searchIndex?.generation !== this.catalog.generation) {
      const generation = this.catalog.generation;
      const words = db ? await new Promise<CatalogSummary[]>((resolve) => {
        const request = db.transaction(INDEX_STORE).objectStore(INDEX_STORE).getAll(generationRange(this.accountId, generation));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => resolve([]);
      }) : [...this.fallbackWords.values()].map((word) => ({ id: word.id, word: word.word,
        search: word.word.toLowerCase(), definition_cn: word.senses[0]?.definition_cn ?? "" }));
      this.searchIndex = { generation, words };
    }
    const search = query.trim().toLowerCase(), result: CatalogSummary[] = [];
    for (const word of this.searchIndex.words) {
      if (!search || word.search.includes(search)) result.push(word);
      if (result.length >= limit) break;
    }
    return result;
  }

  // Only metadata is restored at startup or rewritten on 304. New content is
  // staged in bounded batches and published after its complete JSON validates.
  syncCatalog(appVersion: string, force = false): Promise<void> {
    if (this.catalogSync) return this.catalogSync;
    this.catalogSync = (async () => {
      await this.loadCatalog();
      const db = await database();
      if (db) this.catalog = await readRecord<Catalog>(db, CATALOG_STORE, this.accountId) ?? this.catalog;
      if (!force && this.catalog?.appVersion === appVersion && Date.now() - this.catalog.checkedAt < 6 * 60 * 60000) return;
      const response = await fetch("/api/study/meaning-catalog", {
        credentials: "include", cache: "no-store",
        headers: this.catalog ? { "If-None-Match": this.catalog.etag } : {}
      });
      if (response.status === 304 && this.catalog) {
        const checked = { ...this.catalog, checkedAt: Date.now(), appVersion };
        this.catalog = checked;
        if (db) await new Promise<void>((resolve) => {
          const tx = db.transaction(CATALOG_STORE, "readwrite");
          const current = tx.objectStore(CATALOG_STORE).get(this.accountId);
          current.onsuccess = () => {
            if (current.result?.generation === checked.generation) tx.objectStore(CATALOG_STORE).put(checked);
            else if (current.result) this.catalog = current.result as Catalog;
          };
          tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
        });
      } else if (response.ok) {
        this.catalog = await this.storeCatalog(streamCatalogWords(response), {
          etag: response.headers.get("ETag") ?? "", checkedAt: Date.now(), appVersion
        });
      } else throw new ApiError(response.status, "词义目录核对失败");
      if (db) await this.pruneInterruptedDownloads(db);
      if (!persistenceRequested) {
        persistenceRequested = true;
        if (typeof navigator !== "undefined") void navigator.storage?.persist?.().catch(() => {});
      }
    })().finally(() => { this.catalogSync = undefined; });
    return this.catalogSync;
  }

  async get(wordId: number, targetUnitId: number | null): Promise<CachedMeanings | undefined> {
    const key = this.key(wordId, targetUnitId);
    const inMemory = this.entries.get(key);
    if (inMemory) {
      this.entries.delete(key);
      if (inMemory.expires > Date.now()) {
        this.entries.set(key, inMemory);
        return { content: inMemory.content, complete: true };
      }
    }
    const db = await database();
    const rich = db && await new Promise<StudyDetails | undefined>((resolve) => {
      try {
        const transaction = db.transaction(STORE, "readwrite");
        const store = transaction.objectStore(STORE);
        let found: Entry | undefined;
        store.get(key).onsuccess = (event) => {
          const entry = (event.target as IDBRequest<Entry | undefined>).result;
          if (!entry) return;
          if (entry.expires <= Date.now()) store.delete(key);
          else {
            found = { ...entry, accessedAt: Date.now() };
            store.put(found);
          }
        };
        transaction.oncomplete = () => {
          if (found) this.remember(found);
          resolve(found?.content);
        };
        transaction.onerror = transaction.onabort = () => resolve(undefined);
      } catch { resolve(undefined); }
    });
    if (rich) return { content: rich, complete: true };
    if (targetUnitId !== null) return undefined;
    const word = await this.savedWord(wordId);
    if (!word) return undefined;
    return { complete: false, content: {
      word: word.word,
      senses: word.senses.map((sense) => ({ ...sense, status: "New", next_review_date: null })),
      entries: [],
      morphology: { inflected_forms: [], derived_words: [], related_words: [] },
      exposed_sense_ids: []
    } };
  }

  async set(wordId: number, targetUnitId: number | null, content: StudyDetails): Promise<void> {
    const now = Date.now();
    const entry = { key: this.key(wordId, targetUnitId), accountId: this.accountId,
      content, expires: now + this.ttl, accessedAt: now };
    this.remember(entry);
    const db = await database();
    if (!db) return;
    if (!persistenceRequested) {
      persistenceRequested = true;
      if (typeof navigator !== "undefined") void navigator.storage?.persist?.().catch(() => {});
    }
    await new Promise<void>((resolve) => {
      try {
        const transaction = db.transaction(STORE, "readwrite");
        const store = transaction.objectStore(STORE);
        store.put(entry);
        store.index("by_account_access").count(this.range()).onsuccess = (event) => {
          let surplus = (event.target as IDBRequest<number>).result - this.limit;
          if (surplus <= 0) return;
          store.index("by_account_access").openCursor(this.range()).onsuccess = (cursorEvent) => {
            const cursor = (cursorEvent.target as IDBRequest<IDBCursorWithValue | null>).result;
            if (cursor && surplus-- > 0) {
              cursor.delete();
              cursor.continue();
            }
          };
        };
        transaction.oncomplete = transaction.onerror = transaction.onabort = () => resolve();
      } catch { resolve(); }
    });
  }

  async clear(): Promise<void> {
    this.entries.clear();
    const db = await database();
    if (!db) return;
    await new Promise<void>((resolve) => {
      try {
        const transaction = db.transaction(STORE, "readwrite");
        transaction.objectStore(STORE).index("by_account_access").openCursor(this.range()).onsuccess = (event) => {
          const cursor = (event.target as IDBRequest<IDBCursorWithValue | null>).result;
          if (cursor) {
            cursor.delete();
            cursor.continue();
          }
        };
        transaction.oncomplete = transaction.onerror = transaction.onabort = () => resolve();
      } catch { resolve(); }
    });
  }
}
