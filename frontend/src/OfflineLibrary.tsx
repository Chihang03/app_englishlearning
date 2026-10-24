import { useEffect, useMemo, useState } from "react";
import { StudyContentCache } from "./studyContentCache";
import type { CatalogWord, CatalogSummary } from "./studyContentCache";
import type { User } from "./types";
import { chineseMeaning, learningMeanings } from "./learningMeanings";

export function OfflineLibrary({ user, onRetry }: { user: User; onRetry: () => void }) {
  const cache = useMemo(() => new StudyContentCache(user.id), [user.id]);
  const [words, setWords] = useState<CatalogSummary[]>([]);
  const [ready, setReady] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogWord | null>(null);
  useEffect(() => {
    let active = true;
    void cache.searchWords(query).then((saved) => {
      if (active) { setWords(saved); setReady(true); }
    }).catch(() => { if (active) { setWords([]); setReady(true); } });
    return () => { active = false; };
  }, [cache, query]);
  return <main className="page-container offline-library">
    <header className="section-heading">
      <h1>{selected ? selected.word : "离线词义"}</h1>
      <button type="button" className="text-button" onClick={onRetry}>重新连接</button>
    </header>
    {selected ? <>
      <button type="button" className="secondary-button" onClick={() => setSelected(null)}>返回词表</button>
      {learningMeanings(selected.senses).map((sense) => <section key={sense.id} className="panel offline-sense">
        <h2>{sense.part_of_speech} · {chineseMeaning(sense.definition_cn)}</h2>
        {sense.definition_en ? <p>{sense.definition_en}</p> : null}
        {sense.semantic_notes ? <p>{sense.semantic_notes}</p> : null}
        {sense.examples.map((example) => <div key={example.id}>
          <p>{example.sentence}</p>
          {example.translation_cn ? <p>{example.translation_cn}</p> : null}
        </div>)}
      </section>)}
    </> : <>
      <input className="offline-search" type="search" aria-label="搜索缓存单词" placeholder="搜索单词"
        value={query} onChange={(event) => setQuery(event.target.value)} />
      {!ready ? <p role="status">加载中…</p> : words.length === 0 ? <p role="status">{query.trim() ? "未找到单词" : "暂无缓存词义"}</p> : <ul className="offline-words">
          {words.map((word) => <li key={word.id}>
            <button type="button" onClick={() => { void cache.savedWord(word.id).then((value) => { if (value) setSelected(value); }).catch(() => {}); }}>
              <strong>{word.word}</strong><span>{word.definition_cn}</span>
            </button>
          </li>)}
        </ul>}
    </>}
  </main>;
}
