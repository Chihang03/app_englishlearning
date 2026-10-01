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
type Catalog = { accountId: number; etag: string; checkedAt: number; appVersion: string; words: CatalogWord[] };
export type CachedMeanings = { content: StudyDetails; complete: boolean };

const STORE = "meanings";
const CATALOG_STORE = "catalog";
let databasePromise: Promise<IDBDatabase | null> | undefined;
let persistenceRequested = false;

function database(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  if (!databasePromise) databasePromise = new Promise((resolve) => {
    try {
      const request = indexedDB.open("cvt-study-content-v1", 2);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) {
          const store = request.result.createObjectStore(STORE, { keyPath: "key" });
          store.createIndex("by_account_access", ["accountId", "accessedAt"]);
        }
        if (!request.result.objectStoreNames.contains(CATALOG_STORE)) {
          request.result.createObjectStore(CATALOG_STORE, { keyPath: "accountId" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
      request.onblocked = () => resolve(null);
    } catch { resolve(null); }
  });
  return databasePromise;
}

// Display data only. Exposure uploads are kept separately and block grading.
export class StudyContentCache {
  private entries = new Map<string, Entry>();
  private catalog: Catalog | null = null;
  private catalogIndex = new Map<number, CatalogWord>();
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
      if (!db) return;
      const saved = await new Promise<Catalog | undefined>((resolve) => {
        try {
          const request = db.transaction(CATALOG_STORE).objectStore(CATALOG_STORE).get(this.accountId);
          request.onsuccess = () => resolve(request.result as Catalog | undefined);
          request.onerror = () => resolve(undefined);
        } catch { resolve(undefined); }
      });
      if (saved) this.useCatalog(saved);
    })();
    return this.catalogLoaded;
  }

  private useCatalog(catalog: Catalog) {
    this.catalog = catalog;
    this.catalogIndex = new Map(catalog.words.map((word) => [word.id, word]));
  }

  async savedWords(): Promise<CatalogWord[]> {
    await this.loadCatalog();
    return this.catalog?.words ?? [];
  }

  // ETag hashes the actual account-visible definitions and examples. An app
  // release with unchanged dictionary content receives 304 and keeps its copy.
  syncCatalog(appVersion: string, force = false): Promise<void> {
    if (this.catalogSync) return this.catalogSync;
    this.catalogSync = (async () => {
      await this.loadCatalog();
      if (!force && this.catalog?.appVersion === appVersion &&
          Date.now() - this.catalog.checkedAt < 6 * 60 * 60000) return;
      const response = await fetch("/api/study/meaning-catalog", {
        credentials: "include", cache: "no-store",
        headers: this.catalog ? { "If-None-Match": this.catalog.etag } : {}
      });
      if (response.status === 304 && this.catalog) {
        this.catalog.checkedAt = Date.now();
        this.catalog.appVersion = appVersion;
      } else if (response.ok) {
        const payload = await response.json() as { words: CatalogWord[] };
        if (!Array.isArray(payload.words)) throw new Error("词义目录格式错误");
        const etag = response.headers.get("ETag") ?? "";
        const changed = this.catalog?.etag !== etag;
        this.useCatalog({ accountId: this.accountId, etag, checkedAt: Date.now(), appVersion, words: payload.words });
        if (changed) await this.clear();
      } else {
        throw new ApiError(response.status, "词义目录核对失败");
      }
      const db = await database();
      if (db && this.catalog) await new Promise<void>((resolve) => {
        try {
          const tx = db.transaction(CATALOG_STORE, "readwrite");
          tx.objectStore(CATALOG_STORE).put(this.catalog);
          tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
        } catch { resolve(); }
      });
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
    await this.loadCatalog();
    const word = this.catalogIndex.get(wordId);
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
