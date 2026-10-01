const API_BASE = "";
export type ApiWrite = { path: string; method: string; body?: BodyInit | null };
const writeListeners = new Set<(write: ApiWrite) => void>();
let pendingWrites = 0;

export function onApiWrite(listener: (write: ApiWrite) => void) {
  writeListeners.add(listener);
  return () => { writeListeners.delete(listener); };
}

export function hasPendingWrites() {
  return pendingWrites > 0;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// FastAPI reports validation problems as a list of per-field objects and
// everything else as a plain string, so both shapes are flattened here.
function readDetail(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (typeof item?.msg === "string" ? item.msg : null))
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length > 0) return messages.join("; ");
  }
  return fallback;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  // /api/next allocates a server attempt, despite its GET transport.
  const writing = !["GET", "HEAD"].includes(method) || path === "/api/next";
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timeout = setTimeout(abort, 15000);
  init?.signal?.addEventListener("abort", abort, { once: true });
  if (init?.signal?.aborted) abort();
  if (writing) pendingWrites++;
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      ...init,
      signal: controller.signal,
      cache: "no-store"
    });

    if (!response.ok) {
      let message = response.statusText;
      try {
        message = readDetail(await response.json(), message);
      } catch {
        // A non-JSON error body leaves the status text as the message.
      }
      throw new ApiError(response.status, message);
    }
    return await response.json() as T;
  } finally {
    clearTimeout(timeout);
    init?.signal?.removeEventListener("abort", abort);
    if (writing) {
      pendingWrites--;
      // Even a lost response may have reached the server. Invalidate on all
      // write completions, including speech settings saved by another component.
      for (const listener of writeListeners) listener({ path, method, body: init?.body });
    }
  }
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
