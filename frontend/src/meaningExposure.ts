import { request } from "./api";
import type { StudyDetails } from "./types";

type Exposure = { attemptId: string; targetUnitId: number | null };
export type MeaningResponse = StudyDetails | Pick<StudyDetails, "exposed_sense_ids">;

// The server remains authoritative for grading. Keep a tiny, account-scoped
// outbox so an immediately displayed cached meaning cannot become an
// independent answer after a network failure or app restart.
export class MeaningExposureQueue {
  private pending = new Map<string, Exposure>();
  private inFlight = new Map<string, Promise<MeaningResponse>>();
  private storageKey: string;

  constructor(accountId: number) {
    this.storageKey = `cvt.meaning-exposure.v1.${accountId}`;
    try {
      const stored = JSON.parse(localStorage.getItem(this.storageKey) ?? "[]");
      if (Array.isArray(stored)) for (const item of stored) {
        if (typeof item?.attemptId === "string" && item.attemptId.length > 0 && item.attemptId.length <= 100 &&
            (item.targetUnitId === null || Number.isSafeInteger(item.targetUnitId) && item.targetUnitId > 0)) {
          this.pending.set(this.key(item), item);
        }
      }
    } catch { /* Private browsing may not provide local storage. */ }
  }

  private key(item: Exposure) { return `${item.attemptId}:${item.targetUnitId ?? "main"}`; }

  private persist() {
    try {
      if (this.pending.size) localStorage.setItem(this.storageKey, JSON.stringify([...this.pending.values()]));
      else localStorage.removeItem(this.storageKey);
      return true;
    } catch { return false; }
  }

  private send(item: Exposure, contentCached: boolean): Promise<MeaningResponse> {
    const key = this.key(item);
    const existing = this.inFlight.get(key);
    if (existing) return existing;
    const task = request<MeaningResponse>("/api/study/meanings", {
      method: "POST", body: JSON.stringify({ attempt_id: item.attemptId,
        target_lexical_unit_id: item.targetUnitId, content_cached: contentCached })
    }).then((payload) => {
      this.pending.delete(key);
      this.persist();
      return payload;
    }).finally(() => { this.inFlight.delete(key); });
    this.inFlight.set(key, task);
    return task;
  }

  expose(attemptId: string, targetUnitId: number | null, contentCached: boolean) {
    const item = { attemptId, targetUnitId };
    this.pending.set(this.key(item), item);
    const durable = this.persist();
    return { durable, response: this.send(item, contentCached) };
  }

  async flush(attemptId?: string): Promise<void> {
    for (const item of [...this.pending.values()]) {
      if (attemptId === undefined || item.attemptId === attemptId) await this.send(item, true);
    }
  }
}
