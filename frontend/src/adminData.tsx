import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage, isUnauthorized, request } from "./api";
import { useForegroundRefresh } from "./lifecycle";
import type { User } from "./types";

export type AdminSession = { onSignedOut: () => void; onSessionChanged: (user: User) => void };

export function useAdminRead<T>(path: string, { onSignedOut, onSessionChanged }: AdminSession, refreshAllowed = true) {
  const [data, setData] = useState<T | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setBusy(true);
    setError("");
    try {
      const result = await request<T>(path, { signal: next.signal });
      if (!next.signal.aborted) setData(result);
    } catch (caught) {
      if (next.signal.aborted) return;
      if (isUnauthorized(caught)) onSignedOut();
      else if (caught instanceof ApiError && caught.status === 403) {
        setData(null);
        try {
          const session = await request<{ user: User }>("/api/auth/me", { signal: next.signal });
          if (!next.signal.aborted) onSessionChanged(session.user);
        } catch (sessionError) {
          if (!next.signal.aborted) {
            if (isUnauthorized(sessionError)) onSignedOut();
            else setError(errorMessage(sessionError));
          }
        }
      } else setError(errorMessage(caught));
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }, [path, onSignedOut, onSessionChanged]);
  useEffect(() => {
    setData(null);
    void refresh();
    return () => controller.current?.abort();
  }, [refresh]);
  useForegroundRefresh(() => { if (refreshAllowed) void refresh(); });
  const cancel = () => { controller.current?.abort(); setBusy(false); };
  return { data, setData, busy, error, refresh, cancel };
}

export const PAGE_SIZE = 50;
export const reportCategories: Record<string, string> = {
  definition: "单词释义错误", sentence: "例句错误", translation: "句子翻译错误",
  pronunciation: "发音问题", other: "其他问题"
};
export function adminDate(value: string | null, timezone: string) {
  return value ? new Date(value).toLocaleString("zh-CN", {
    timeZone: timezone, year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"
  }) : "暂无";
}

export function AdminChevron() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>;
}

export function AdminHeader({ title, onBack, busy, disabled = false, onRefresh }: {
  title: string; onBack?: () => void; busy: boolean; disabled?: boolean; onRefresh: () => void;
}) {
  return <header className="page-header admin-header">
    {onBack ? <button type="button" className="icon-button" aria-label="返回" onClick={onBack}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M20 12H4m6-6-6 6 6 6" /></svg>
    </button> : null}
    <h1>{title}</h1>
    <button type="button" className="secondary-button" disabled={busy || disabled} onClick={onRefresh}>{busy ? "刷新中…" : "刷新"}</button>
  </header>;
}

export function AdminPagination({ offset, total, limit, busy, onOffset }: {
  offset: number; total: number; limit: number; busy: boolean; onOffset: (offset: number) => void;
}) {
  if (total <= limit) return null;
  return <div className="admin-pagination">
    <button type="button" className="secondary-button" disabled={busy || offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>上一页</button>
    <span>{Math.floor(offset / limit) + 1} / {Math.ceil(total / limit)}</span>
    <button type="button" className="secondary-button" disabled={busy || offset + limit >= total} onClick={() => onOffset(offset + limit)}>下一页</button>
  </div>;
}
