import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { errorMessage, isUnauthorized, request } from "./api";
import type { Card, Sense } from "./types";

export type StudyTool = "meanings" | "report";

const reportTypes = [
  { value: "definition", label: "单词释义错误" },
  { value: "sentence", label: "例句错误" },
  { value: "translation", label: "句子翻译错误" },
  { value: "pronunciation", label: "发音问题" },
  { value: "other", label: "其他问题" }
] as const;

export function StudyTools({ tool, card, showTranslation, onClose, onSignedOut, onAnswerExposed }: {
  tool: StudyTool; card: Card; showTranslation: boolean;
  onClose: () => void; onSignedOut: () => void; onAnswerExposed: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const activeRef = useRef(true);
  const submittingRef = useRef(false);
  const [meanings, setMeanings] = useState<{ word: string; senses: Sense[] } | null>(null);
  const [category, setCategory] = useState<string>(reportTypes[0].value);
  const [details, setDetails] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [reportId, setReportId] = useState<number | null>(null);
  const [loading, setLoading] = useState(tool === "meanings");

  useEffect(() => {
    activeRef.current = true;
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => { activeRef.current = false; dialog?.close(); };
  }, []);

  async function loadMeanings() {
    setLoading(true);
    setError("");
    try {
      const payload = await request<{ word: string; senses: Sense[] }>("/api/study/meanings", {
        method: "POST", body: JSON.stringify({ attempt_id: card.attempt_id })
      });
      if (!activeRef.current) return;
      onAnswerExposed();
      setMeanings(payload);
    } catch (caught) {
      if (!activeRef.current) return;
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally {
      if (activeRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (tool === "meanings") void loadMeanings();
  }, []);

  async function submitReport(event: FormEvent) {
    event.preventDefault();
    if (submittingRef.current || reportId !== null) return;
    submittingRef.current = true;
    setSaving(true);
    setError("");
    try {
      const payload = await request<{ id: number }>("/api/content-reports", {
        method: "POST", body: JSON.stringify({ attempt_id: card.attempt_id, category, details })
      });
      if (activeRef.current) setReportId(payload.id);
    } catch (caught) {
      if (!activeRef.current) return;
      if (isUnauthorized(caught)) onSignedOut();
      else setError(errorMessage(caught));
    } finally {
      submittingRef.current = false;
      if (activeRef.current) setSaving(false);
    }
  }

  return (
    <dialog ref={dialogRef} className="study-dialog" aria-labelledby="study-dialog-title"
      onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <header className="study-dialog-header">
        <h2 id="study-dialog-title">{tool === "meanings" ? "更多词义" : "报告错误"}</h2>
        <button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>×</button>
      </header>
      <div className="study-dialog-content">
        {tool === "meanings" ? (
          <>
            {loading ? <p role="status">正在加载词义…</p> : null}
            {meanings ? <>
              <p className="dictionary-word">{meanings.word}</p>
              {meanings.senses.length === 1 ? <p className="sense-hint">词库暂时只收录了这个词义。</p> : null}
              {meanings.senses.length === 0 ? <p className="sense-hint">词库暂时没有可显示的词义。</p> : null}
              {meanings.senses.map((sense) => (
                <section className="other-sense" key={sense.id}>
                  <span className="part-of-speech">{sense.part_of_speech}{sense.id === card.sense_id ? " · 当前词义" : ""}</span>
                  <p>{sense.definition_cn || sense.definition_en}</p>
                  {sense.examples.map((example) => <p className="other-sense-example" key={example.id}>
                    {example.sentence}{showTranslation && example.translation_cn ? <span>{example.translation_cn}</span> : null}
                  </p>)}
                </section>
              ))}
            </> : null}
            {error ? <><p role="alert" className="error-notice">{error}</p>
              <button type="button" className="secondary-button" disabled={loading} onClick={() => { void loadMeanings(); }}>重试</button></> : null}
          </>
        ) : reportId !== null ? (
          <div className="report-success" role="status">
            <p>报告已提交，感谢你的反馈。</p>
            <p className="sense-hint">报告编号 #{reportId}</p>
            <button type="button" className="primary-button" onClick={onClose}>继续学习</button>
          </div>
        ) : (
          <form className="report-form" onSubmit={submitReport}>
            <div className="report-context">
              <p>{card.definition_cn || card.definition_en}</p>
              <p>{card.cloze_sentence}</p>
            </div>
            <fieldset disabled={saving}>
              <legend>哪里有问题？</legend>
              <div className="report-types">{reportTypes.map((type) => (
                <label key={type.value} className="report-type">
                  <input type="radio" name="report-type" value={type.value} checked={category === type.value}
                    onChange={() => setCategory(type.value)} />
                  <span>{type.label}</span>
                </label>
              ))}</div>
              <label className="report-details">补充说明（选填）
                <textarea value={details} onChange={(event) => setDetails(event.target.value)} maxLength={2000}
                  rows={3} placeholder="可以描述问题，或写下你认为正确的内容" />
              </label>
            </fieldset>
            {error ? <p className="error-notice" role="alert">{error}</p> : null}
            <button type="submit" className="primary-button" disabled={saving}>{saving ? "正在提交…" : "提交报告"}</button>
          </form>
        )}
      </div>
    </dialog>
  );
}
