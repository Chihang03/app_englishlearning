import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

const API_BASE = "";

type Card = {
  id: number;
  part_of_speech: string;
  definition_cn: string;
  definition_en?: string | null;
  cloze_sentence: string;
  example_sentence: string;
  example_translation_cn?: string | null;
  status: "New" | "Learning" | "Reviewing" | "Mature";
  remaining_today: number;
};

type ReviewResult = {
  is_correct: boolean;
  is_blank: boolean;
  correct_answer: string;
  example_sentence: string;
  srs_state: {
    correct_count: number;
    interval_days: number;
    next_review_date: string;
    status: Card["status"];
  };
};

type Stats = {
  today_learning: number;
  today_accuracy: number;
  total_learned: number;
  due_review: number;
  new_words: number;
  learning: number;
  learning_due: number;
  lapse_words: number;
  due_lapses: number;
  mastered: number;
  mature: number;
  streak_days: number;
};

type Voice = {
  name: string;
  locale: string;
  description: string;
};

type Settings = {
  voice: string;
  rate: number;
  show_sentence_translation: boolean;
  voices: Voice[];
};

const emptyStats: Stats = {
  today_learning: 0,
  today_accuracy: 0,
  total_learned: 0,
  due_review: 0,
  new_words: 0,
  learning: 0,
  learning_due: 0,
  lapse_words: 0,
  due_lapses: 0,
  mastered: 0,
  mature: 0,
  streak_days: 0
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || response.statusText);
  }
  return response.json() as Promise<T>;
}

function highlightSentence(sentence: string, word: string) {
  if (!word) return sentence;
  const escaped = word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`\\b(${escaped})\\b`, "gi");
  return sentence.split(regex).map((part, index) =>
    part.toLowerCase() === word.toLowerCase() ? (
      <mark key={`${part}-${index}`} className="rounded bg-amber-200 px-1 py-0.5 text-gray-950">
        {part}
      </mark>
    ) : (
      <span key={`${part}-${index}`}>{part}</span>
    )
  );
}

function App() {
  const [card, setCard] = useState<Card | null>(null);
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [settings, setSettings] = useState<Settings>({
    voice: "Samantha",
    rate: 175,
    show_sentence_translation: false,
    voices: []
  });
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const visibleSentence = useMemo(() => {
    if (result) {
      return highlightSentence(result.example_sentence, result.correct_answer);
    }
    return card?.cloze_sentence ?? "";
  }, [card, result]);

  async function loadStats() {
    const nextStats = await request<Stats>("/api/stats");
    setStats(nextStats);
  }

  async function loadSettings() {
    const nextSettings = await request<Settings>("/api/settings");
    setSettings(nextSettings);
  }

  async function loadNext() {
    setLoading(true);
    setResult(null);
    setAnswer("");
    const payload = await request<{ card: Card | null; message?: string }>("/api/next");
    setCard(payload.card);
    setMessage(payload.message ?? "");
    setLoading(false);
    window.setTimeout(() => inputRef.current?.focus(), 50);
  }

  async function refreshAll() {
    await Promise.all([loadStats(), loadSettings(), loadNext()]);
  }

  async function speak(text: string) {
    if (!text.trim()) return;
    await request<{ status: string }>("/api/tts", {
      method: "POST",
      body: JSON.stringify({ text })
    });
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!card || submitting || result?.is_correct || (result && answer.trim() === "")) return;
    setSubmitting(true);
    try {
      const review = await request<ReviewResult>("/api/review", {
        method: "POST",
        body: JSON.stringify({ word_id: card.id, user_answer: answer })
      });
      setResult(review);
      await loadStats();
      if (review.is_correct) {
        await speak(review.example_sentence);
        window.setTimeout(() => {
          loadNext();
        }, 450);
      } else {
        setAnswer("");
        window.setTimeout(() => inputRef.current?.focus(), 50);
        void speak(review.example_sentence).catch((error) => setMessage(error.message));
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleAnswerChange(value: string) {
    if (result && !result.is_correct && value !== "") {
      setResult(null);
    }
    setAnswer(value);
  }

  async function updateSetting(next: Partial<Settings>) {
    const payload = await request<Settings>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(next)
    });
    setSettings(payload);
  }

  useEffect(() => {
    refreshAll().catch((error) => {
      setMessage(error.message);
      setLoading(false);
    });
  }, []);

  const blankSpeech = card?.cloze_sentence.replace(/_______/g, "blank") ?? "";

  return (
    <main className="min-h-screen bg-[#f7f7f4] px-4 py-5 text-gray-950 sm:px-6 lg:px-8">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <header className="flex flex-col gap-4 border-b border-gray-300 pb-4 md:flex-row md:items-end md:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-normal">Context Vocabulary Trainer</h1>
            <p className="mt-1 text-sm text-gray-600">通过语境回忆单词，而不是孤立背诵。</p>
          </div>
          <div className="grid grid-cols-4 gap-2 text-center sm:grid-cols-8">
            <Stat label="今日" value={stats.today_learning} />
            <Stat label="正确率" value={`${stats.today_accuracy}%`} />
            <Stat label="累计" value={stats.total_learned} />
            <Stat label="待复习" value={stats.due_review} />
            <Stat label="新词" value={stats.new_words} />
            <Stat label="学习中" value={stats.learning} />
            <Stat label="错词" value={stats.lapse_words} />
            <Stat label="已掌握" value={stats.mastered} />
          </div>
        </header>

        <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_280px]">
          <div className="flex min-h-[520px] flex-col justify-center border border-gray-300 bg-white px-5 py-6 shadow-sm sm:px-8">
            {loading ? (
              <p className="text-gray-500">Loading...</p>
            ) : card ? (
              <>
                <div className="mb-7 flex flex-wrap items-center gap-2 text-sm">
                  <span className="border border-gray-300 px-2.5 py-1 text-gray-700">{card.status}</span>
                  <span className="text-gray-500">{queueLabel(card)}</span>
                </div>

                <div className="space-y-5">
                  <div className="border-l-4 border-gray-950 bg-[#fff7df] px-4 py-3">
                    <div className="text-xs font-semibold uppercase text-gray-600">{card.part_of_speech}</div>
                    <div className="mt-1 text-2xl font-semibold leading-snug text-gray-950">
                      {card.definition_cn}
                    </div>
                  </div>
                  <p className="max-w-3xl text-3xl font-semibold leading-snug tracking-normal text-gray-950 sm:text-4xl">
                    {visibleSentence}
                  </p>
                  {settings.show_sentence_translation && card.example_translation_cn ? (
                    <p className="max-w-3xl text-base leading-7 text-gray-600">
                      {card.example_translation_cn}
                    </p>
                  ) : null}
                  <div className="min-h-[72px]">
                    {result?.is_correct ? (
                      <ResultLine tone="correct" text="Correct" />
                    ) : result ? (
                      <div className="space-y-2">
                        <ResultLine
                          tone="incorrect"
                          text={result.is_blank ? "不会" : "Incorrect"}
                        />
                        <p className="text-base text-gray-700">
                          正确答案：<span className="font-semibold text-gray-950">{result.correct_answer}</span>
                        </p>
                      </div>
                    ) : null}
                  </div>
                </div>

                <form onSubmit={submit} className="mt-8 flex flex-col gap-3 sm:flex-row">
                  <input
                    ref={inputRef}
                    value={answer}
                    onChange={(event) => handleAnswerChange(event.target.value)}
                    disabled={submitting || Boolean(result?.is_correct)}
                    className="h-12 min-w-0 flex-1 border border-gray-300 bg-white px-4 text-lg outline-none transition focus:border-gray-950 disabled:bg-gray-100"
                    placeholder={result && !result.is_correct ? "继续输入可重试" : "输入答案，空白回车表示不会"}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    type="submit"
                    disabled={submitting || Boolean(result?.is_correct)}
                    className="h-12 border border-gray-950 bg-gray-950 px-6 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-300"
                  >
                    {submitting ? "处理中" : "提交"}
                  </button>
                  <button
                    type="button"
                    onClick={() => speak(result ? result.example_sentence : blankSpeech)}
                    className="h-12 border border-gray-300 px-5 text-sm font-medium text-gray-900"
                  >
                    发音
                  </button>
                </form>
              </>
            ) : (
              <div className="space-y-3">
                <p className="text-2xl font-semibold">今日没有待复习单词</p>
                <p className="text-gray-600">{message}</p>
              </div>
            )}
          </div>

          <aside className="border border-gray-300 bg-white px-4 py-5 shadow-sm">
            <h2 className="text-sm font-semibold text-gray-900">显示设置</h2>
            <div className="mt-4 space-y-4">
              <label className="flex items-center justify-between gap-3 text-sm text-gray-700">
                <span>句子中文释义</span>
                <input
                  type="checkbox"
                  checked={settings.show_sentence_translation}
                  onChange={(event) => updateSetting({ show_sentence_translation: event.target.checked })}
                  className="h-5 w-5 accent-gray-950"
                />
              </label>
            </div>

            <h2 className="mt-7 text-sm font-semibold text-gray-900">发音设置</h2>
            <div className="mt-4 space-y-4">
              <label className="block text-sm text-gray-600">
                语音
                <select
                  value={settings.voice}
                  onChange={(event) => updateSetting({ voice: event.target.value })}
                  className="mt-1 h-10 w-full border border-gray-300 bg-white px-2 text-gray-950"
                >
                  {settings.voices.length === 0 ? (
                    <option value={settings.voice}>{settings.voice}</option>
                  ) : (
                    settings.voices.map((voice) => (
                      <option key={`${voice.name}-${voice.locale}`} value={voice.name}>
                        {voice.name} · {voice.locale}
                      </option>
                    ))
                  )}
                </select>
              </label>

              <label className="block text-sm text-gray-600">
                语速 {settings.rate}
                <input
                  type="range"
                  min="80"
                  max="320"
                  value={settings.rate}
                  onChange={(event) => setSettings({ ...settings, rate: Number(event.target.value) })}
                  onBlur={() => updateSetting({ rate: settings.rate })}
                  className="mt-2 w-full"
                />
              </label>

              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => updateSetting({ voice: preferredVoice(settings.voices, "en_US") })}
                  className="h-10 border border-gray-300 text-sm"
                >
                  美式
                </button>
                <button
                  type="button"
                  onClick={() => updateSetting({ voice: preferredVoice(settings.voices, "en_GB") })}
                  className="h-10 border border-gray-300 text-sm"
                >
                  英式
                </button>
              </div>
            </div>
          </aside>
        </section>
      </div>
    </main>
  );
}

function preferredVoice(voices: Voice[], locale: "en_US" | "en_GB") {
  return voices.find((voice) => voice.locale === locale)?.name ?? voices[0]?.name ?? "Samantha";
}

function queueLabel(card: Card) {
  if (card.status === "New") {
    return "新词学习，不设每日上限";
  }
  if (card.remaining_today > 0) {
    return `错词待复习 ${card.remaining_today}`;
  }
  return "记忆曲线回顾";
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-[72px] border border-gray-300 bg-white px-2 py-2">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-1 text-lg font-semibold leading-none">{value}</div>
    </div>
  );
}

function ResultLine({ tone, text }: { tone: "correct" | "incorrect"; text: string }) {
  const color = tone === "correct" ? "text-emerald-700" : "text-red-700";
  const mark = tone === "correct" ? "✓" : "✗";
  return <p className={`text-lg font-semibold ${color}`}>{mark} {text}</p>;
}

export default App;
