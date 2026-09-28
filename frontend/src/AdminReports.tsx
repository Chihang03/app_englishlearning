import { useEffect, useRef, useState } from "react";
import { errorMessage, isUnauthorized, request } from "./api";
import { AdminHeader, AdminPagination, AdminChevron, adminDate, PAGE_SIZE, reportCategories, useAdminRead } from "./adminData";
import type { AdminSession } from "./adminData";

type Status = "pending" | "resolved";
type Report = { id: number; category: string; details: string; created_at: string; status: Status; username: string; word: string };
type ReportPage = { reports: Report[]; total: number };
type Detail = Omit<Report, "word"> & {
  resolution_notes: string; resolved_at: string | null; resolved_by: string | null;
  content: { word: string; part_of_speech: string; definition_cn: string | null; definition_en: string | null;
    sentence: string; translation_cn: string | null; pronunciation: string | null; target_form: string };
};

export function AdminReports({ session, timezone, offset, onOffset, status, onStatus, onBack, onReport }: {
  session: AdminSession; timezone: string; offset: number; onOffset: (offset: number) => void;
  status: Status; onStatus: (status: Status) => void; onBack: () => void; onReport: (id: number) => void;
}) {
  const { data, busy, error, refresh } = useAdminRead<ReportPage>(`/api/admin/reports?status=${status}&offset=${offset}&limit=${PAGE_SIZE}`, session);
  return <>
    <AdminHeader title="反馈" onBack={onBack} busy={busy} onRefresh={() => { void refresh(); }} />
    <div className="admin-filters" role="group" aria-label="反馈状态">
      <button type="button" className="secondary-button" aria-pressed={status === "pending"} onClick={() => onStatus("pending")}>待处理</button>
      <button type="button" className="secondary-button" aria-pressed={status === "resolved"} onClick={() => onStatus("resolved")}>已处理</button>
    </div>
    {error ? <p className="warning-notice" role="alert">{error}</p> : null}
    <section className="settings-group panel admin-list" aria-label="反馈列表" aria-busy={busy}>
      {data ? data.reports.length ? <ul>{data.reports.map(report => <li key={report.id}>
        <button type="button" className="admin-list-row" onClick={() => onReport(report.id)}>
          <div><h2>{report.word}</h2><p>{reportCategories[report.category]}</p>
            <dl><div><dt>提交用户</dt><dd>{report.username}</dd></div><div><dt>提交时间</dt><dd>{adminDate(report.created_at, timezone)}</dd></div></dl>
          </div><AdminChevron />
        </button>
      </li>)}</ul> : <p>暂无反馈</p> : <p role="status">{error ? "暂时无法加载" : "加载中…"}</p>}
      {data ? <AdminPagination offset={offset} total={data.total} limit={PAGE_SIZE} busy={busy} onOffset={onOffset} /> : null}
    </section>
  </>;
}

export function AdminReportDetail({ id, session, timezone, onBack }: { id: number; session: AdminSession; timezone: string; onBack: () => void }) {
  const [notes, setNotes] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const savingRef = useRef(false);
  const active = useRef(true);
  const { data, setData, busy, error, refresh, cancel } = useAdminRead<Detail>(`/api/admin/reports/${id}`, session, !saving && !dirty);
  useEffect(() => { if (data && !dirty) setNotes(data.resolution_notes); }, [data, dirty]);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  async function save(status: Status) {
    if (savingRef.current || !data) return;
    savingRef.current = true;
    cancel();
    setSaving(true);
    setSaveError("");
    try {
      const result = await request<Detail>(`/api/admin/reports/${id}`, { method: "PATCH", body: JSON.stringify({ status, resolution_notes: notes }) });
      if (active.current) { setData(result); setDirty(false); }
    } catch (caught) {
      if (active.current) {
        if (isUnauthorized(caught)) session.onSignedOut();
        else setSaveError(errorMessage(caught));
      }
    } finally {
      savingRef.current = false;
      if (active.current) setSaving(false);
    }
  }

  return <>
    <AdminHeader title="反馈详情" onBack={saving ? undefined : onBack} busy={busy || saving || dirty} onRefresh={() => { void refresh(); }} />
    {error || saveError ? <p className="warning-notice" role="alert">{saveError || error}</p> : null}
    {data ? <>
      <section className="settings-group panel admin-detail"><h2>{reportCategories[data.category]}</h2><dl>
        <div><dt>提交用户</dt><dd>{data.username}</dd></div><div><dt>提交时间</dt><dd>{adminDate(data.created_at, timezone)}</dd></div>
        <div><dt>状态</dt><dd>{data.status === "pending" ? "待处理" : "已处理"}</dd></div>
        {data.resolved_at ? <div><dt>处理时间</dt><dd>{adminDate(data.resolved_at, timezone)}</dd></div> : null}
      </dl><p className="admin-report-text">{data.details || "无补充说明"}</p></section>
      <section className="settings-group panel admin-detail admin-content"><h2>{data.content.word}</h2>
        <p>{data.content.part_of_speech}</p>
        {data.content.definition_cn ? <p>{data.content.definition_cn}</p> : null}
        {data.content.definition_en ? <p>{data.content.definition_en}</p> : null}
        <p>{data.content.sentence}</p>
        {data.content.translation_cn ? <p>{data.content.translation_cn}</p> : null}
        {data.content.pronunciation ? <p>{data.content.pronunciation}</p> : null}
      </section>
      <section className="settings-group panel admin-detail"><label className="admin-notes"><strong>处理备注</strong>
        <textarea aria-label="处理备注" rows={4} maxLength={2000} value={notes} disabled={saving}
          onChange={event => { setNotes(event.target.value); setDirty(true); }} />
      </label>
        <div className="admin-report-actions">
          {dirty ? <button type="button" className="secondary-button" disabled={saving} onClick={() => { void save(data.status); }}>保存备注</button> : null}
          <button type="button" className="primary-button" disabled={saving}
            onClick={() => { void save(data.status === "pending" ? "resolved" : "pending"); }}>
            {saving ? "保存中…" : data.status === "pending" ? "标记已处理" : "重新打开"}
          </button>
        </div>
      </section>
    </> : !error ? <p className="mt-6" role="status">加载中…</p> : null}
  </>;
}
