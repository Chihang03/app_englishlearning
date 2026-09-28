import type { FormEvent, ReactNode, Ref } from "react";
import { InlineAnswer } from "./InlineAnswer";
import type { Card, ReviewResult } from "./types";

type Props = {
  card: Card | null;
  result: ReviewResult | null;
  answer: string;
  showTranslation: boolean;
  marks: ReactNode;
  soundIcon: ReactNode;
  message: string;
  loading?: boolean;
  submitting?: boolean;
  readingCorrectAnswer?: boolean;
  busy?: boolean;
  wordHintDisabled?: boolean;
  readOnly?: boolean;
  inputRef?: Ref<HTMLTextAreaElement>;
  questionRef?: Ref<HTMLDivElement>;
  onSubmit?: (event: FormEvent) => void;
  onEnter?: () => void;
  onWordHint: () => void;
  onAnswerChange?: (value: string) => void;
};

// Both live and completed cards use the same presentation. A previous card
// freezes the completed round's data; only its editing behavior changes.
export function StudyCard({ card, result, answer, showTranslation, marks, soundIcon, message,
  loading = false, submitting = false, readingCorrectAnswer = false, busy = false,
  wordHintDisabled = false, readOnly = false, inputRef, questionRef,
  onSubmit, onEnter, onWordHint, onAnswerChange }: Props) {
  const split = (card?.cloze_sentence ?? "_______").split("_______");
  const parts = split.length > 1 ? split : [...split, ""];
  return <form onSubmit={readOnly ? (event) => event.preventDefault() : onSubmit}
    className={`question-card${readOnly ? " previous-question" : ""}`}
    aria-busy={loading} aria-label={readOnly ? "上一题，仅回看" : "当前题目"}>
    {marks}
    <div className="question-heading">
      <div className="question-labels">
        <span className="pill">{!card ? "加载中…" : card.status === "New" && !card.needs_correction ? (card.is_new_word ? "新词" : "新用法") : "复习"}</span>
        <span className="part-of-speech">{card?.part_of_speech}{card?.answer_form_label ? ` · ${card.answer_form_label}` : ""}</span>
      </div>
      <button type="button" className="icon-button pronunciation-button" onClick={onWordHint}
        disabled={wordHintDisabled} aria-label={readOnly ? "重新朗读上一题句子" : "朗读单词"} title={readOnly ? "朗读句子" : "朗读单词"}>
        {soundIcon}
      </button>
    </div>
    <div ref={questionRef} className="question-content" tabIndex={0} aria-label="英文句子">
      <p className="english-sentence">
        {parts.flatMap((part, index) => [
          <span key={`text-${index}`}>{part}</span>,
          index < parts.length - 1 ? <InlineAnswer key={`blank-${index}`} id={!readOnly && index === 0 ? "study-answer" : undefined}
            ref={index === 0 ? inputRef : undefined}
            className={`sentence-input ${result?.is_correct ? "is-correct" : result || card?.needs_correction ? "is-retry" : ""}`}
            value={result ? result.correct_answer : answer} readOnly={readOnly} tabIndex={readOnly ? -1 : undefined}
            onEnter={readOnly ? () => {} : onEnter}
            onChange={(event) => { if (!busy && !readOnly) onAnswerChange?.(event.target.value.replace(/[\r\n]+/g, " ")); }}
            onFocus={(event) => { if (!readOnly && result && !result.is_correct) event.currentTarget.select(); }}
            onClick={(event) => { if (!readOnly && result && !result.is_correct) event.currentTarget.select(); }}
            aria-label={index === 0 ? "输入英文答案" : `输入英文答案，第 ${index + 1} 处挖空`}
            aria-busy={busy} aria-invalid={!result?.is_correct && (Boolean(result) || Boolean(card?.needs_correction))}
            autoComplete="off" autoCorrect="off" autoCapitalize="none" spellCheck={false}
            enterKeyHint="send" inputMode="text" lang="en" /> : null
        ])}
      </p>
    </div>
    <div className="meaning-block" hidden={!card} tabIndex={0} aria-label="单词释义与句子翻译">
      <p className="word-meaning">{card?.definition_cn || card?.definition_en}</p>
      {showTranslation && card?.example_translation_cn ? <p className="sentence-translation">{card.example_translation_cn}</p> : null}
    </div>
    {message ? <p role="alert" className="error-notice">{message}</p> : null}
    <span className="sr-only" role="status">{submitting ? "提交中" : loading ? "正在加载下一题…" : readingCorrectAnswer ? "整句朗读中，结束后自动进入下一题…" : ""}</span>
  </form>;
}
