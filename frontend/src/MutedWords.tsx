import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent } from "react";
import { errorMessage, isUnauthorized } from "./api";
import type { ReadCache } from "./readCache";

export function MutedWords({ onRestore, onSignedOut, readCache }: {
  onRestore: (word: string) => Promise<void>; onSignedOut: () => void; readCache: ReadCache;
}) {
  const [words, setWords] = useState<{ word: string; muted_at: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef(false);
  const activeRef = useRef(true);
  const [error, setError] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const groupRefs = useRef<Record<string, HTMLElement | null>>({});
  const dragPointerRef = useRef<number | null>(null);
  const [activeLetter, setActiveLetter] = useState("");
  const [dragging, setDragging] = useState(false);
  const groups = useMemo(() => {
    const grouped = new Map<string, typeof words>();
    for (const item of words) {
      const initial = item.word.charAt(0).toUpperCase();
      const letter = /^[A-Z]$/.test(initial) ? initial : "#";
      const group = grouped.get(letter) ?? [];
      group.push(item);
      grouped.set(letter, group);
    }
    return grouped;
  }, [words]);
  const letters = [..."ABCDEFGHIJKLMNOPQRSTUVWXYZ", ...(groups.has("#") ? ["#"] : [])];
  const availableLetters = letters.filter((letter) => groups.has(letter));

  useEffect(() => {
    if (!groups.has(activeLetter)) setActiveLetter(availableLetters[0] ?? "");
  }, [groups, activeLetter]);

  function jumpToLetter(letter: string) {
    const index = letters.indexOf(letter);
    const target = availableLetters.find((available) => letters.indexOf(available) >= index)
      ?? availableLetters[availableLetters.length - 1];
    const list = listRef.current;
    const group = groupRefs.current[target];
    if (!list || !group) return;
    setActiveLetter(target);
    const top = list.scrollTop + group.getBoundingClientRect().top - list.getBoundingClientRect().top;
    list.scrollTo({ top, behavior: "instant" });
  }

  function dragToLetter(event: PointerEvent<HTMLElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = (event.clientY - bounds.top) / bounds.height;
    const index = Math.max(0, Math.min(letters.length - 1, Math.floor(position * letters.length)));
    jumpToLetter(letters[index]);
  }

  function stopDragging() {
    dragPointerRef.current = null;
    setDragging(false);
  }

  async function load() {
    setLoading(true);
    setError("");
    try {
      await readCache.load<{ words: typeof words }>("/api/muted-words", 5 * 60000, (payload) => {
        if (!activeRef.current) return;
        setWords(payload.words);
        setLoading(false);
      });
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

  return <section className="muted-words-panel" aria-label="不再学习的单词">
    <h2>单词列表{!loading && !error ? ` · ${words.length}` : ""}</h2>
    {loading ? <p className="settings-hint" role="status">加载中…</p> : null}
    {!loading && !error && words.length === 0 ? <p className="settings-hint">暂无单词</p> : null}
    {error ? <><p className="error-notice" role="alert">{error}</p>
      <button type="button" className="secondary-button" disabled={loading || busy !== null} onClick={() => { void load(); }}>重新加载</button></> : null}
    {words.length > 0 ? <div className="muted-word-browser">
      <div ref={listRef} className="muted-words-list" tabIndex={0} aria-label="按首字母排列的单词">
        {availableLetters.map((letter) => <section key={letter} aria-label={`${letter} 开头的单词`}
          ref={(element) => { groupRefs.current[letter] = element; }}>
          <h3 className="muted-letter-heading">{letter}</h3>
          <ul>{groups.get(letter)?.map(({ word }) => <li key={word}>
            <span>{word}</span>
            <button type="button" className="secondary-button" disabled={busy !== null}
              aria-label={`恢复学习 ${word}`} onClick={() => { void restore(word); }}>{busy === word ? "恢复中…" : "恢复学习"}</button>
          </li>)}</ul>
        </section>)}
      </div>
      <nav className="muted-alphabet" aria-label="首字母快速查找"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          dragPointerRef.current = event.pointerId;
          event.currentTarget.setPointerCapture(event.pointerId);
          setDragging(true);
          dragToLetter(event);
        }}
        onPointerMove={(event) => { if (dragPointerRef.current === event.pointerId) dragToLetter(event); }}
        onPointerUp={stopDragging} onPointerCancel={stopDragging} onLostPointerCapture={stopDragging}>
        {letters.map((letter) => <button type="button" key={letter}
          className={activeLetter === letter ? "is-active" : ""}
          aria-label={`跳至 ${letter} 开头的单词`} aria-disabled={!groups.has(letter)}
          aria-current={activeLetter === letter ? "true" : undefined} tabIndex={groups.has(letter) ? 0 : -1}
          onClick={(event) => { if (event.detail === 0) jumpToLetter(letter); }}>{letter}</button>)}
      </nav>
      {dragging && activeLetter ? <span className="muted-letter-preview" role="status">{activeLetter}</span> : null}
    </div> : null}
  </section>;
}
