import { useEffect, useMemo, useState } from "react";
import { StudyContentCache } from "./studyContentCache";
import type { CatalogWord } from "./studyContentCache";
import type { User } from "./types";

export function OfflineLibrary({ user, onRetry }: { user: User; onRetry: () => void }) {
  const cache = useMemo(() => new StudyContentCache(user.id), [user.id]);
  const [words, setWords] = useState<CatalogWord[]>([]);
  const [ready, setReady] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogWord | null>(null);
  useEffect(() => {
    let active = true;
    void cache.savedWords().then((saved) => {
      if (active) { setWords(saved); setReady(true); }
    });
    return () => { active = false; };
  }, [cache]);
  const visible = useMemo(() => {
    const search = query.trim().toLowerCase();
    return words.filter((word) => !search || word.word.toLowerCase().includes(search)).slice(0, 50);
  }, [words, query]);

  return <main className="page-container offline-library">
    <header className="section-heading">
      <h1>{selected ? selected.word : "离线词义"}</h1>
      <button type="button" className="text-button" onClick={onRetry}>重新连接</button>
    </header>
    {selected ? <>
      <button type="button" className="secondary-button" onClick={() => setSelected(null)}>返回词表</button>
      {selected.senses.map((sense) => <section key={sense.id} className="panel offline-sense">
        <h2>{sense.part_of_speech} · {sense.definition_cn}</h2>
        {sense.definition_en ? <p>{sense.definition_en}</p> : null}
        {sense.examples.map((example) => <div key={example.id}>
          <p>{example.sentence}</p>
          {example.translation_cn ? <p>{example.translation_cn}</p> : null}
        </div>)}
      </section>)}
    </> : <>
      <input className="offline-search" type="search" aria-label="搜索缓存单词" placeholder="搜索单词"
        value={query} onChange={(event) => setQuery(event.target.value)} />
      {!ready ? <p role="status">加载中…</p> : words.length === 0 ? <p role="status">暂无缓存词义</p>
        : visible.length === 0 ? <p role="status">未找到单词</p> : <ul className="offline-words">
          {visible.map((word) => <li key={word.id}>
            <button type="button" onClick={() => setSelected(word)}>
              <strong>{word.word}</strong><span>{word.senses[0]?.definition_cn}</span>
            </button>
          </li>)}
        </ul>}
    </>}
  </main>;
}
