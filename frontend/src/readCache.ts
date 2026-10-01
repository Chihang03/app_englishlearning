import type { ApiWrite } from "./api";

// Display snapshots only. Attempts, hints and answers never belong here.
type Entry = { value?: unknown; expires: number; pending?: Promise<unknown> };
const SNAPSHOT_PATHS = ["/api/stats", "/api/settings", "/api/word-lists", "/api/muted-words"];
let databasePromise: Promise<IDBDatabase | null> | undefined;

function database(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  databasePromise ??= new Promise((resolve) => {
    try {
      const request = indexedDB.open("cvt-read-snapshots-v1", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("snapshots");
      request.onsuccess = () => resolve(request.result);
      request.onerror = request.onblocked = () => resolve(null);
    } catch { resolve(null); }
  });
  return databasePromise;
}

export class ReadCache {
  private entries = new Map<string, Entry>();
  private revisions = new Map<string, number>();
  private storageQueue: Promise<void> = Promise.resolve();

  constructor(private fetcher: <T>(path: string) => Promise<T>, private accountId?: number) {}

  private revision(path: string) { return this.revisions.get(path) ?? 0; }
  private key(path: string) { return `${this.accountId}:${path}`; }
  private persistent(path: string) { return this.accountId !== undefined && SNAPSHOT_PATHS.includes(path); }

  private persist(path: string, value?: unknown) {
    if (!this.persistent(path)) return;
    // Serialize storage operations so delayed puts cannot resurrect deleted snapshots.
    this.storageQueue = this.storageQueue.then(async () => {
      const db = await database();
      if (!db) return;
      await new Promise<void>((resolve) => {
        try {
          const tx = db.transaction("snapshots", "readwrite");
          const store = tx.objectStore("snapshots");
          if (value === undefined) store.delete(this.key(path));
          else store.put(value, this.key(path));
          tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
        } catch { resolve(); }
      });
    }).catch(() => {});
  }

  invalidate(paths: string[]) {
    for (const path of paths) {
      this.revisions.set(path, this.revision(path) + 1);
      this.entries.delete(path);
      this.persist(path);
    }
  }

  clear() { this.invalidate([...new Set([...SNAPSHOT_PATHS, ...this.entries.keys()])]); }

  invalidateWrite({ path, body }: ApiWrite) {
    if (path === "/api/settings") {
      let changed: Record<string, unknown> | undefined;
      try {
        const parsed = typeof body === "string" ? JSON.parse(body) : null;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) changed = parsed;
      } catch { /* Invalidate conservatively. */ }
      this.invalidate(changed && !("skip_basic_600" in changed) && !("selected_word_list_ids" in changed)
        ? ["/api/settings"] : ["/api/settings", "/api/stats", "/api/word-lists"]);
    } else if (path === "/api/review" || path === "/api/next") {
      this.invalidate(["/api/stats", "/api/word-lists"]);
    } else if (path === "/api/hint" || path.startsWith("/api/study/")) {
      this.invalidate(["/api/stats"]);
    } else if (/^\/api\/words\/\d+\/mute$/.test(path) || path.startsWith("/api/muted-words/")) {
      this.invalidate(["/api/stats", "/api/word-lists", "/api/muted-words"]);
    } else if (path.startsWith("/api/push/") || path.startsWith("/api/auth/passkeys/") || path === "/api/content-reports") {
      // These writes do not change any selected display resources.
    } else {
      this.clear();
    }
  }

  async peek<T>(path: string): Promise<T | undefined> {
    const current = this.entries.get(path);
    if (current?.value !== undefined) return current.value as T;
    if (!this.persistent(path)) return undefined;
    const revision = this.revision(path);
    await this.storageQueue;
    const db = await database();
    if (!db) return undefined;
    const value = await new Promise<unknown>((resolve) => {
      try {
        const request = db.transaction("snapshots").objectStore("snapshots").get(this.key(path));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => resolve(undefined);
      } catch { resolve(undefined); }
    });
    if (revision !== this.revision(path)) return undefined;
    const latest = this.entries.get(path);
    if (latest) {
      // A sibling (for example speech settings) may already be revalidating.
      // Its pending read should not prevent the durable snapshot from painting.
      if (latest.value === undefined && value !== undefined) latest.value = value;
      return latest.value as T | undefined;
    }
    if (value !== undefined) this.entries.set(path, { value, expires: 0 });
    return value as T | undefined;
  }

  // Expired snapshots paint the screen while the server is checked.
  // Forced refreshes expose errors rather than quietly settling for old data.
  async load<T>(path: string, ttl: number, display: (value: T) => void, force = false) {
    const revision = this.revision(path);
    const snapshot = force ? undefined : await this.peek<T>(path);
    if (snapshot !== undefined && revision === this.revision(path)) display(snapshot);
    try { display(await this.get<T>(path, ttl, force)); }
    catch (error) {
      if (snapshot === undefined || force || revision !== this.revision(path) ||
          (error as { status?: number })?.status === 401) throw error;
    }
  }

  async get<T>(path: string, ttl: number, force = false): Promise<T> {
    let entry = this.entries.get(path);
    if (entry?.pending) return entry.pending as Promise<T>;
    if (!force && entry && entry.expires > Date.now()) return entry.value as T;
    entry = { value: entry?.value, expires: 0 };
    this.entries.set(path, entry);
    const current = entry;
    current.pending = this.fetcher<T>(path).then((value) => {
      if (this.entries.get(path) !== current) return this.get<T>(path, ttl);
      current.value = value;
      current.expires = Date.now() + ttl;
      this.persist(path, value);
      return value;
    }).finally(() => {
      if (this.entries.get(path) === current) current.pending = undefined;
    });
    return current.pending as Promise<T>;
  }
}
