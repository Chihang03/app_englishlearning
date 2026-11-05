import { useEffect, useState } from "react";
import { StudyContentCache } from "./studyContentCache";
import { errorMessage, isUnauthorized } from "./api";
import { version } from "./version.json";

export function OfflineContentSettings({ cache, onSignedOut }: { cache: StudyContentCache; onSignedOut: () => void }) {
  const [status, setStatus] = useState<Awaited<ReturnType<StudyContentCache["status"]>>>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    let active = true;
    const refresh = () => { void cache.status().then(value => { if (active) setStatus(value); }).catch(() => {}); };
    const unsubscribe = cache.subscribe(refresh); refresh();
    return () => { active = false; unsubscribe(); };
  }, [cache]);
  async function run(clear: boolean) {
    setBusy(true); setMessage("");
    try {
      if (clear) await cache.removeDownloads();
      else await cache.syncCatalog(version, true);
      setStatus(await cache.status());
    } catch (error) {
      if (isUnauthorized(error)) onSignedOut();
      else setMessage(errorMessage(error));
    } finally { setBusy(false); }
  }
  const working = busy || status?.syncing;
  return <section className="settings-group panel" aria-label="离线内容">
    <h2>离线内容</h2>
    <dl className="detail-metrics">
      <div><dt>已下载单词</dt><dd>{status ? status.words.toLocaleString() : "—"}</dd></div>
      {status?.checkedAt ? <div><dt>最近核对</dt><dd>{new Date(status.checkedAt).toLocaleString()}</dd></div> : null}
      {status?.usage !== undefined ? <div><dt>应用存储占用（估算）</dt><dd>{(status.usage / 1024 / 1024).toFixed(1)} MB</dd></div> : null}
      {status ? <div><dt>存储保留</dt><dd>{!status.available ? "不可用" : status.persistent ? "优先保留" : "由系统管理"}</dd></div> : null}
    </dl>
    <div className="voice-shortcuts">
      <button type="button" className="secondary-button" disabled={working || status?.available === false} onClick={() => { void run(false); }}>{working ? "更新中…" : "更新离线内容"}</button>
      <button type="button" className="secondary-button" disabled={working || status?.available === false} onClick={() => { void run(true); }}>清理离线内容</button>
    </div>
    {message || status?.error ? <p role="alert" className="error-notice">{message || status?.error}</p> : null}
  </section>;
}
