import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { errorMessage, isUnauthorized, request, ApiError } from "./api";
import { useForegroundRefresh } from "./lifecycle";
import { version } from "./version.json";
import type { User } from "./types";

type Overview = {
  users: number; today_active_users: number; reviews: number;
  today_reviews: number; public_words: number; pending_reports: number;
};
type Learner = {
  id: number; username: string; timezone: string; created_at: string;
  reviews: number; last_review_at: string | null;
};
type UserPage = { users: Learner[]; total: number; offset: number; limit: number };
const PAGE_SIZE = 50;

export function AdminDashboard({ user, onSignedOut, onSessionChanged, accountSecurity, updateAvailable }: {
  user: User; onSignedOut: () => void; onSessionChanged: (user: User) => void;
  accountSecurity: ReactNode; updateAvailable: boolean;
}) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [users, setUsers] = useState<UserPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [endingSession, setEndingSession] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setBusy(true);
    setError("");
    try {
      const [summary, page] = await Promise.all([
        request<Overview>("/api/admin/overview", { signal: next.signal }),
        request<UserPage>(`/api/admin/users?offset=${offset}&limit=${PAGE_SIZE}`, { signal: next.signal })
      ]);
      if (next.signal.aborted) return;
      setOverview(summary);
      setUsers(page);
    } catch (caught) {
      if (next.signal.aborted) return;
      if (isUnauthorized(caught)) onSignedOut();
      else if (caught instanceof ApiError && caught.status === 403) {
        setOverview(null);
        setUsers(null);
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
  }, [offset, onSignedOut, onSessionChanged]);

  useEffect(() => {
    void refresh();
    return () => controller.current?.abort();
  }, [refresh]);
  useForegroundRefresh(() => { void refresh(); });

  async function signOut() {
    setEndingSession(true);
    setError("");
    try {
      await request("/api/auth/logout", { method: "POST" });
      onSignedOut();
    } catch (caught) {
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally {
      setEndingSession(false);
    }
  }

  const date = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", {
    timeZone: user.timezone, year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"
  }) : "暂无";
  const metrics: [keyof Overview, string][] = [
    ["users", "用户总数"], ["today_active_users", "今日学习人数"],
    ["reviews", "累计作答"], ["today_reviews", "今日作答"],
    ["public_words", "公共词库单词"], ["pending_reports", "待处理反馈"]
  ];

  return <main className="admin-shell">
    <div className="page-container admin-page">
      <header className="page-header">
        <h1>管理概览</h1>
        <button type="button" className="secondary-button" disabled={busy || endingSession} onClick={() => { void refresh(); }}>
          {busy ? "刷新中…" : "刷新"}
        </button>
      </header>
      {error ? <p className="warning-notice" role="alert">{error}</p> : null}
      {updateAvailable ? <button type="button" className="secondary-button admin-update" onClick={() => window.location.reload()}>更新应用</button> : null}
      <section className="overview-grid admin-metrics" aria-label="基础信息" aria-busy={busy}>
        {metrics.map(([key, label]) => <div className="metric-card panel" key={key}>
          <strong>{overview ? overview[key].toLocaleString() : "—"}</strong><span className="metric-label">{label}</span>
        </div>)}
      </section>
      <section className="settings-group panel admin-users" aria-label="用户列表">
        <h2>用户</h2>
        {users ? users.users.length ? <ul>
          {users.users.map(learner => <li key={learner.id}>
            <h3>{learner.username}</h3>
            <dl>
              <div><dt>注册时间</dt><dd>{date(learner.created_at)}</dd></div>
              <div><dt>累计作答</dt><dd>{learner.reviews.toLocaleString()}</dd></div>
              <div><dt>最近学习</dt><dd>{date(learner.last_review_at)}</dd></div>
            </dl>
          </li>)}
        </ul> : <p>暂无用户</p> : <p role="status">{error ? "暂时无法加载" : "加载中…"}</p>}
        {users && users.total > PAGE_SIZE ? <div className="admin-pagination">
          <button type="button" className="secondary-button" disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
          <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(users.total / PAGE_SIZE)}</span>
          <button type="button" className="secondary-button" disabled={busy || offset + PAGE_SIZE >= users.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
        </div> : null}
      </section>
      <details className="learning-details panel admin-security">
        <summary><span>账号安全</span></summary>
        <div className="pb-4">{accountSecurity}</div>
      </details>
      <button type="button" className="sign-out-button secondary-button" disabled={endingSession} onClick={() => { void signOut(); }}>退出登录</button>
      <p className="app-version">版本 {version}</p>
    </div>
  </main>;
}
