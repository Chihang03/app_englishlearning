// Only explicitly selected read-only data belongs here. /api/next is a write.
type Entry = { value?: unknown; expires: number; pending?: Promise<unknown> };

export class ReadCache {
  private entries = new Map<string, Entry>();

  constructor(private fetcher: <T>(path: string) => Promise<T>) {}

  clear() {
    this.entries.clear();
  }

  async get<T>(path: string, ttl: number, force = false): Promise<T> {
    let entry = this.entries.get(path);
    if (entry?.pending) return entry.pending as Promise<T>;
    if (!force && entry && entry.expires > Date.now()) return entry.value as T;
    entry = { expires: 0 };
    this.entries.set(path, entry);
    const current = entry;
    current.pending = this.fetcher<T>(path).then((value) => {
      // A write may have invalidated this request while it was in flight.
      // Its old response must neither refill the cache nor replace newer UI data.
      if (this.entries.get(path) !== current) return this.get<T>(path, ttl);
      current.value = value;
      current.expires = Date.now() + ttl;
      return value;
    }).finally(() => {
      if (this.entries.get(path) === current) current.pending = undefined;
    });
    return current.pending as Promise<T>;
  }
}
