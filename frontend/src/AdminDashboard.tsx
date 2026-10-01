import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { errorMessage, isUnauthorized, request } from "./api";
import { AdminUsers, AdminUserDetail } from "./AdminUsers";
import { AdminReports, AdminReportDetail } from "./AdminReports";
import { AdminChevron, AdminHeader, useAdminRead } from "./adminData";
import type { AdminSession } from "./adminData";
import { version } from "./version.json";
import { activateAppUpdate } from "./appWorker";
import type { User } from "./types";

type Overview = {
  users: number; today_active_users: number; reviews: number;
  today_reviews: number; public_words: number; pending_reports: number;
};
type Page = "admin" | "admin/users" | "admin/reports" | `admin/users/${number}` | `admin/reports/${number}`;
function pageFromHash(): Page {
  const value = window.location.hash.slice(1);
  return /^admin(?:\/(?:users|reports)(?:\/[1-9]\d*)?)?$/.test(value) ? value as Page : "admin";
}

function AdminOverview({ session, onNavigate }: { session: AdminSession; onNavigate: (page: Page) => void }) {
  const { data, busy, error, refresh } = useAdminRead<Overview>("/api/admin/overview", session);
  const metrics: [keyof Overview, string][] = [
    ["users", "用户总数"], ["today_active_users", "今日学习人数"],
    ["reviews", "累计作答"], ["today_reviews", "今日作答"], ["public_words", "公共词库单词"]
  ];
  return <>
    <AdminHeader title="管理概览" busy={busy} onRefresh={() => { void refresh(); }} />
    {error ? <p className="warning-notice" role="alert">{error}</p> : null}
    <section className="overview-grid admin-metrics admin-overview-metrics" aria-label="基础信息" aria-busy={busy}>
      {metrics.map(([key, label]) => <div className="metric-card panel" key={key}>
        <strong>{data ? data[key].toLocaleString() : "—"}</strong><span className="metric-label">{label}</span>
      </div>)}
    </section>
    <nav className="admin-menu" aria-label="管理入口">
      <button type="button" className="panel admin-menu-row" onClick={() => onNavigate("admin/users")}><strong>用户</strong><AdminChevron /></button>
      <button type="button" className="panel admin-menu-row" onClick={() => onNavigate("admin/reports")}>
        <strong>待处理反馈</strong>{data && data.pending_reports > 0 ? <span className="admin-badge">{data.pending_reports.toLocaleString()}</span> : null}<AdminChevron />
      </button>
    </nav>
  </>;
}

export function AdminDashboard({ user, onSignedOut, onSessionChanged, accountSecurity, updateAvailable }: {
  user: User; onSignedOut: () => void; onSessionChanged: (user: User) => void;
  accountSecurity: ReactNode; updateAvailable: boolean;
}) {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [userOffset, setUserOffset] = useState(0);
  const [reportOffset, setReportOffset] = useState(0);
  const [reportStatus, setReportStatus] = useState<"pending" | "resolved">("pending");
  const [endingSession, setEndingSession] = useState(false);
  const [error, setError] = useState("");
  const session = { onSignedOut, onSessionChanged };
  const navigate = (next: Page) => { window.location.hash = next; };
  useEffect(() => {
    if (window.location.hash.slice(1) !== pageFromHash()) window.history.replaceState(null, "", "#admin");
    const change = () => { setPage(pageFromHash()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);

  async function signOut() {
    setEndingSession(true);
    setError("");
    try {
      await request("/api/auth/logout", { method: "POST" });
      onSignedOut();
    } catch (caught) {
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally { setEndingSession(false); }
  }

  return <main className="admin-shell"><div className="page-container admin-page">
    {page === "admin" ? <AdminOverview session={session} onNavigate={navigate} /> : null}
    {page === "admin/users" ? <AdminUsers session={session} timezone={user.timezone} offset={userOffset} onOffset={setUserOffset}
      onBack={() => navigate("admin")} onUser={id => navigate(`admin/users/${id}`)} /> : null}
    {page.startsWith("admin/users/") ? <AdminUserDetail key={page} id={Number(page.split("/")[2])} session={session}
      timezone={user.timezone} onBack={() => navigate("admin/users")} /> : null}
    {page === "admin/reports" ? <AdminReports session={session} timezone={user.timezone} offset={reportOffset} onOffset={setReportOffset}
      status={reportStatus} onStatus={status => { setReportStatus(status); setReportOffset(0); }}
      onBack={() => navigate("admin")} onReport={id => navigate(`admin/reports/${id}`)} /> : null}
    {page.startsWith("admin/reports/") ? <AdminReportDetail key={page} id={Number(page.split("/")[2])} session={session}
      timezone={user.timezone} onBack={() => navigate("admin/reports")} /> : null}
    {error ? <p className="warning-notice" role="alert">{error}</p> : null}
    {page === "admin" ? <>
      {updateAvailable ? <button type="button" className="secondary-button admin-update" onClick={() => {
        void activateAppUpdate().then(() => window.location.reload()).catch(() => {});
      }}>更新应用</button> : null}
      <details className="learning-details panel admin-security"><summary><span>账号安全</span><AdminChevron /></summary><div className="pb-4">{accountSecurity}</div></details>
      <button type="button" className="sign-out-button secondary-button" disabled={endingSession} onClick={() => { void signOut(); }}>退出登录</button>
      <p className="app-version">版本 {version}</p>
    </> : null}
  </div></main>;
}
