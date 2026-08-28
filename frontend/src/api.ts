const API_BASE = "";

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
  const response = await fetch(`${API_BASE}${path}`, {
    // The session lives in a cookie, which must ride along even when the
    // frontend is served from a different origin than the API.
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init
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

  return response.json() as Promise<T>;
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
