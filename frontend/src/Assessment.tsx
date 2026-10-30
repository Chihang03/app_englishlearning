import { useEffect, useRef, useState, type ReactNode } from "react";
import { request, isUnauthorized } from "./api";

type Result = { label: string; range: string[]; bands: { band: number; answered: number; correct: number }[];
  recall: { answered: number; correct: number }; sufficient: boolean; boundary: boolean };
type Session = { id: string; state: "active" | "completed" | "abandoned"; answered: number; maximum: number;
  question: { sequence: number; kind: "recognition" | "recall"; prompt: string; options: string[] } | null;
  result: Result | null; applied: boolean };
type Profile = { enabled: boolean; mode: string; profile: unknown; session: { id: string; state: string } | null; labels: string[] };
export function ProficiencySettings({ onOpen, onSignedOut, children }: {
  onOpen: () => void; onSignedOut: () => void; children: ReactNode;
}) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  useEffect(() => { let alive = true; void request<Profile>("/api/proficiency").then(p => { if (alive) setProfile(p); })
    .catch(e => { if (isUnauthorized(e)) onSignedOut(); }); return () => { alive = false; }; }, [onSignedOut]);
  async function change(mode: string) {
    setBusy(true); setError("");
    try { setProfile(await request<Profile>("/api/proficiency/mode", { method: "PATCH", body: JSON.stringify({ mode }) })); }
    catch (e) { if (isUnauthorized(e)) onSignedOut(); else setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <>
    <button type="button" className="word-list-option" onClick={onOpen}><strong>词汇水平测验</strong><span aria-hidden="true">›</span></button>
    {profile?.profile ? <label className="setting-row"><strong>学习方式</strong>
      <select aria-label="学习方式" value={profile.mode} disabled={busy} onChange={e => void change(e.target.value)}>
        <option value="adaptive">按水平调整</option><option value="frequency">按词频学习</option>
      </select></label> : null}
    {profile?.mode !== "adaptive" ? children : null}
    {error ? <p role="alert" className="error-notice">{error}</p> : null}
  </>;
}
export function Assessment({ onBack, onApplied, onSignedOut }: {
  onBack: () => void; onApplied: () => void; onSignedOut: () => void;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [labels, setLabels] = useState<string[]>([]);
  const [busy, setBusy] = useState(true); const [error, setError] = useState("");
  const [answer, setAnswer] = useState("");
  const pending = useRef(false); const live = useRef(true);
  // Persist the submitted request until acknowledgement. A lost response can
  // safely retry the exact answer instead of changing an already-scored item.
  const retry = useRef<{ sequence: number; answer: string } | null>(null);
  useEffect(() => {
    live.current = true;
    void request<Profile>("/api/proficiency").then(async p => {
      if (!live.current) return; setLabels(p.labels);
      if (p.session && p.session.state !== "abandoned") {
        const existing = await request<Session>(`/api/assessments/${p.session.id}`);
        if (live.current) setSession(existing);
      }
    }).catch(e => { if (isUnauthorized(e)) onSignedOut(); else if (live.current) setError(e.message); })
      .finally(() => { if (live.current) setBusy(false); });
    return () => { live.current = false; };
  }, [onSignedOut]);
  async function perform(action: string, body?: unknown) {
    if (pending.current) return; pending.current = true; setBusy(true); setError("");
    try {
      const path = action === "start" ? "/api/assessments" : `/api/assessments/${session!.id}/${action}`;
      const next = await request<Session>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
      if (!live.current) return;
      setSession(next); setAnswer(""); retry.current = null;
      if (action === "apply") onApplied();
    } catch (e) {
      if (isUnauthorized(e)) onSignedOut(); else if (live.current) setError(e instanceof Error ? e.message : "请求失败");
    } finally { pending.current = false; if (live.current) setBusy(false); }
  }
  function submit(value: string) {
    if (!session?.question) return;
    retry.current ??= { sequence: session.question.sequence, answer: value };
    void perform("answers", retry.current);
  }
  const q = session?.state === "active" ? session.question : null;
  const result = session?.result;
  return <section className="settings-page page-container assessment-page" aria-label="词汇水平测验">
    <header className="settings-header"><button type="button" className="icon-button" aria-label="返回设置" onClick={onBack}>‹</button>
      <h1>词汇水平测验</h1><span className="header-spacer" /></header>
    {busy && !session ? <p role="status">正在加载…</p> : null}
    {!busy && !session || session?.state === "abandoned" ? <div className="panel assessment-intro">
      <h2>找到适合你的学习起点</h2><p>28–40 题 · 约 6–10 分钟</p>
      <button type="button" className="primary-button" disabled={busy} onClick={() => void perform("start")}>开始测验</button>
    </div> : null}
    {q ? <div className="panel assessment-card" aria-busy={busy}>
      <p className="assessment-progress" role="status">已完成 {session!.answered} 题</p>
      <h2>{q.kind === "recognition" ? "选择单词的含义" : "输入对应的英文单词"}</h2>
      <p className="assessment-prompt" key={q.sequence}>{q.prompt}</p>
      {q.kind === "recognition" ? <div className="assessment-options">{q.options.map((option, i) =>
        <button type="button" key={`${q.sequence}-${i}`} disabled={busy || !!retry.current} onClick={() => submit(String(i))}>{option}</button>)}</div>
        : <form onSubmit={e => { e.preventDefault(); if (!busy && answer.trim()) submit(answer.trim()); }}>
          <label className="sr-only" htmlFor="assessment-answer">英文答案</label>
          <input id="assessment-answer" value={answer} onChange={e => setAnswer(e.target.value)} autoCapitalize="none" autoCorrect="off"
            spellCheck={false} autoComplete="off" disabled={busy || !!retry.current} onKeyDown={e => { if (e.key === "Enter" && (e.nativeEvent.isComposing || e.keyCode === 229)) e.preventDefault(); }} />
          <button type="submit" className="primary-button" disabled={busy || !!retry.current || !answer.trim()}>提交</button>
        </form>}
      <button type="button" className="assessment-skip" disabled={busy || !!retry.current} onClick={() => submit("")}>不认识</button>
      <button type="button" className="assessment-exit" disabled={busy} onClick={onBack}>退出并保留进度</button>
    </div> : null}
    {session?.state === "completed" && result ? <div className="panel assessment-result">
      <h2>{result.sufficient ? result.label : "暂未取得足够证据"}</h2>
      <p>词汇理解范围：{result.range[0]}{result.range[0] !== result.range[1] ? ` — ${result.range[1]}` : ""}</p>
      <dl className="assessment-bands">{result.bands.map(b => <div key={b.band}><dt>{labels[b.band] ?? `层级 ${b.band + 1}`}</dt>
        <dd>{b.correct} / {b.answered}</dd></div>)}</dl>
      <p>线索提取：{result.recall.correct} / {result.recall.answered}</p>
      {result.boundary ? <p>表现接近本次题库的能力边界，结果范围将保守保留。</p> : null}
      {result.sufficient ? <button type="button" className="primary-button" disabled={busy || session.applied}
        onClick={() => void perform("apply")}>{session.applied ? "已应用到学习" : "按此水平开始"}</button> : null}
      <button type="button" disabled={busy} onClick={() => void perform("start")}>重新测验</button>
    </div> : null}
    {error ? <div role="alert" className="error-notice"><p>{error}</p>
      {retry.current ? <button type="button" disabled={busy} onClick={() => void perform("answers", retry.current)}>重试提交</button> : null}
    </div> : null}
  </section>;
}
