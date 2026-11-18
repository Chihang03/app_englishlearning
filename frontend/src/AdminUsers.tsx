import { useState } from "react";
import { errorMessage, isUnauthorized, request } from "./api";
import { AdminHeader, AdminPagination, AdminChevron, adminDate, PAGE_SIZE, useAdminRead } from "./adminData";
import type { AdminSession } from "./adminData";

type Learner = { id: number; username: string; timezone: string; created_at: string; reviews: number; last_review_at: string | null };
type UserPage = { users: Learner[]; total: number; limit: number };
type Detail = {
  user: Omit<Learner, "reviews" | "last_review_at">;
  activity: { reviews: number; correct_reviews: number; today_reviews: number; last_review_at: string | null;
    learned_words: number; learning_senses: number; reviewing_senses: number; mature_senses: number; muted_words: number; passkeys: number };
  word_lists: { list_id: string; title: string }[];
};

export function AdminUsers({ session, timezone, offset, onOffset, onBack, onUser }: {
  session: AdminSession; timezone: string; offset: number; onOffset: (offset: number) => void;
  onBack: () => void; onUser: (id: number) => void;
}) {
  const { data, busy, error, refresh } = useAdminRead<UserPage>(`/api/admin/users?offset=${offset}&limit=${PAGE_SIZE}`, session);
  return <>
    <AdminHeader title="用户" onBack={onBack} busy={busy} onRefresh={() => { void refresh(); }} />
    {error ? <p className="warning-notice" role="alert">{error}</p> : null}
    <section className="settings-group panel admin-list" aria-label="用户列表" aria-busy={busy}>
      {data ? data.users.length ? <ul>{data.users.map(learner => <li key={learner.id}>
        <button type="button" className="admin-list-row" onClick={() => onUser(learner.id)}>
          <div><h2>{learner.username}</h2><dl><div><dt>累计作答</dt><dd>{learner.reviews.toLocaleString()}</dd></div>
            <div><dt>最近学习</dt><dd>{adminDate(learner.last_review_at, timezone)}</dd></div></dl></div><AdminChevron />
        </button>
      </li>)}</ul> : <p>暂无用户</p> : <p role="status">{error ? "暂时无法加载" : "加载中…"}</p>}
      {data ? <AdminPagination offset={offset} total={data.total} limit={PAGE_SIZE} busy={busy} onOffset={onOffset} /> : null}
    </section>
  </>;
}

export function AdminUserDetail({ id, session, timezone, onBack }: { id: number; session: AdminSession; timezone: string; onBack: () => void }) {
  const { data, busy, error, refresh } = useAdminRead<Detail>(`/api/admin/users/${id}`, session);
  const [reset, setReset] = useState<"idle" | "confirm" | "busy">("idle");
  const [temporary, setTemporary] = useState("");
  const [resetError, setResetError] = useState("");
  async function resetPassword() {
    setReset("busy"); setResetError("");
    try {
      const result = await request<{ temporary_password: string }>(`/api/admin/users/${id}/password`, { method: "POST" });
      setTemporary(result.temporary_password);
    } catch (caught) {
      if (isUnauthorized(caught)) session.onSignedOut();
      else setResetError(errorMessage(caught));
    } finally { setReset("idle"); }
  }
  const activity = data?.activity;
  const metrics = activity ? [
    ["累计作答", activity.reviews.toLocaleString()], ["今日作答", activity.today_reviews.toLocaleString()],
    ["正确率", activity.reviews ? `${Math.round(activity.correct_reviews / activity.reviews * 100)}%` : "—"],
    ["已学习单词", activity.learned_words.toLocaleString()], ["学习中义项", activity.learning_senses.toLocaleString()],
    ["复习中义项", activity.reviewing_senses.toLocaleString()], ["成熟义项", activity.mature_senses.toLocaleString()],
    ["不再学习单词", activity.muted_words.toLocaleString()]
  ] : [];
  return <>
    <AdminHeader title={data?.user.username ?? "用户详情"} onBack={onBack} busy={busy} onRefresh={() => { void refresh(); }} />
    {error ? <p className="warning-notice" role="alert">{error}</p> : null}
    {data && activity ? <>
      <section className="settings-group panel admin-detail"><h2>账号信息</h2><dl>
        <div><dt>注册时间</dt><dd>{adminDate(data.user.created_at, timezone)}</dd></div>
        <div><dt>时区</dt><dd>{data.user.timezone}</dd></div>
        <div><dt>最近学习</dt><dd>{adminDate(activity.last_review_at, timezone)}</dd></div>
        <div><dt>通行密钥</dt><dd>{activity.passkeys}</dd></div>
      </dl></section>
      <section className="overview-grid admin-metrics" aria-label="学习详情">{metrics.map(([label, value]) =>
        <div className="metric-card panel" key={label}><strong>{value}</strong><span className="metric-label">{label}</span></div>)}</section>
      <section className="settings-group panel admin-detail"><h2>重置密码</h2>
        {temporary ? <dl><div><dt>临时密码</dt><dd><code>{temporary}</code></dd></div></dl> : null}
        {resetError ? <p className="warning-notice" role="alert">{resetError}</p> : null}
        {reset === "idle" ? <button type="button" className="secondary-button" onClick={() => setReset("confirm")}>{temporary ? "重新生成" : "生成临时密码"}</button>
          : <div className="voice-shortcuts">
            <button type="button" className="secondary-button" disabled={reset === "busy"} onClick={() => { void resetPassword(); }}>{reset === "busy" ? "处理中…" : "确认重置"}</button>
            <button type="button" className="secondary-button" disabled={reset === "busy"} onClick={() => setReset("idle")}>取消</button>
          </div>}
      </section>
      <section className="settings-group panel admin-detail"><h2>学习词库</h2>
        {data.word_lists.length ? <ul className="admin-word-lists">{data.word_lists.map(list => <li key={list.list_id}>{list.title}</li>)}</ul> : <p>未选择词库</p>}
      </section>
    </> : !error ? <p role="status" className="mt-6">加载中…</p> : null}
  </>;
}
