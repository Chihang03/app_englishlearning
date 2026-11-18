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

// The API reports problems in English. Learners read Chinese, so every message
// shown in the interface passes through this table first.
const exact: Record<string, string> = {
  "Incorrect username or password": "用户名或密码不正确",
  "Incorrect password": "当前密码不正确",
  "Username already taken": "该用户名已被使用",
  "Invalid username": "用户名需为 3–32 位字母、数字或 _ . -",
  "Invalid registration code": "邀请码不正确",
  "Not authenticated": "登录已失效，请重新登录",
  "Session expired": "登录已失效，请重新登录",
  "Administrator access required": "需要管理员权限",
  "Learning is unavailable for administrator accounts": "管理员账号不能用于学习",
  "Not Found": "内容不存在",
  "Unknown endpoint": "内容不存在",
  "Method Not Allowed": "当前操作不可用",
  "Answer too long": "答案过长",
  "Report too long": "补充说明过长",
  "Too many notification devices": "接收提醒的设备已达上限",
  "Learning reminders are unavailable": "学习提醒暂不可用",
  "Notification origin is not allowed": "当前站点不能开启学习提醒",
  "This sense has no usable example": "该词义暂无可用例句",
  "Request data budget exceeded": "服务繁忙，请稍后重试",
  "Unknown word": "单词不存在",
  "Unknown learning unit": "学习内容不存在",
  "Unknown dictionary sense": "词义不存在"
};

const patterns: [RegExp, (match: RegExpMatchArray) => string][] = [
  [/should match pattern/i, () => "用户名需为 3–32 位字母、数字或 _ . -"],
  [/at least (\d+) characters?/i, (match) => `至少需要 ${match[1]} 个字符`],
  [/at most (\d+) characters?/i, (match) => `最多 ${match[1]} 个字符`],
  [/^Unknown timezone/i, () => "无法识别的时区"],
  [/^Field required/i, () => "请填写完整"]
];

function localizedApiMessage(status: number, message: string): string {
  const parts = message.split("; ").map((part) => {
    if (exact[part]) return exact[part];
    for (const [pattern, translate] of patterns) {
      const match = part.match(pattern);
      if (match) return translate(match);
    }
    return part;
  });
  const text = [...new Set(parts)].join("；");
  // Anything still untranslated is replaced by a status-level explanation.
  if (/[㐀-鿿]/.test(text)) return text;
  if (status === 401) return "登录已失效，请重新登录";
  if (status === 403) return "没有权限执行此操作";
  if (status === 404) return "内容不存在";
  if (status === 409) return "当前状态已变化，请刷新后重试";
  if (status === 422 || status === 400) return "提交的内容不符合要求";
  if (status === 429 || status === 503) return "服务繁忙，请稍后重试";
  return "服务暂时不可用，请稍后重试";
}

function localizedFailure(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") return "请求超时，请重试";
  // fetch rejects with a TypeError when the network or server is unreachable.
  if (error instanceof TypeError) return "网络连接不可用，请检查网络后重试";
  if (error instanceof Error) return /[㐀-鿿]/.test(error.message) ? error.message : "操作未能完成，请重试";
  return "操作未能完成，请重试";
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
      throw new ApiError(response.status, localizedApiMessage(response.status, message));
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
  return error instanceof ApiError ? error.message : localizedFailure(error);
}
