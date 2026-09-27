import { useEffect, useRef, useState } from "react";
import { errorMessage, isUnauthorized, request } from "./api";

export function MutedWords({ onRestore, onSignedOut }: {
  onRestore: (word: string) => Promise<void>; onSignedOut: () => void;
}) {
  const [words, setWords] = useState<{ word: string; muted_at: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef(false);
  const activeRef = useRef(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const payload = await request<{ words: typeof words }>("/api/muted-words");
      if (activeRef.current) setWords(payload.words);
    } catch (caught) {
      if (!activeRef.current) return;
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally {
      if (activeRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    activeRef.current = true;
    void load();
    return () => { activeRef.current = false; };
  }, []);

  async function restore(word: string) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(word);
    setError("");
    try {
      await onRestore(word);
      if (activeRef.current) setWords((current) => current.filter((item) => item.word !== word));
    } catch (caught) {
      if (!activeRef.current) return;
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally {
      busyRef.current = false;
      if (activeRef.current) setBusy(null);
    }
  }

  return <section className="settings-group panel" aria-label="不再学习的单词">
    <h2>单词列表{!loading && !error ? ` · ${words.length}` : ""}</h2>
    <p className="settings-hint">这些单词的所有义项都不会再出题，学习记录仍然保留。</p>
    {loading ? <p className="settings-hint" role="status">加载中…</p> : null}
    {!loading && !error && words.length === 0 ? <p className="settings-hint">暂无单词</p> : null}
    {error ? <><p className="error-notice" role="alert">{error}</p>
      <button type="button" className="secondary-button" disabled={loading || busy !== null} onClick={() => { void load(); }}>重新加载</button></> : null}
    <ul className="muted-words-list">{words.map(({ word }) => <li key={word}>
      <span>{word}</span>
      <button type="button" className="secondary-button" disabled={busy !== null}
        aria-label={`恢复学习 ${word}`} onClick={() => { void restore(word); }}>{busy === word ? "恢复中…" : "恢复学习"}</button>
    </li>)}</ul>
  </section>;
}
