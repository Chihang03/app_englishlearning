import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AuthScreen } from "./AuthScreen";
import { PasskeySettings } from "./PasskeySettings";
import { errorMessage, isUnauthorized, request } from "./api";
import type { Card, ReviewResult, Settings, SpeechSettings, Stats, User } from "./types";

// The old backend shelled out to macOS `say -r`, which took words per minute and
// defaulted to 175. SpeechSynthesisUtterance instead takes a multiplier where 1
// is normal speed, so the slider keeps the familiar wpm range and converts on the
// way out.
const BASE_WPM = 175;
const MIN_WPM = 80;
const MAX_WPM = 320;

// Voice and speed are per-browser now, so they live in localStorage rather than
// in the server-side settings table.
const SPEECH_SETTINGS_KEY = "cvt.speech";

// How long a correct answer stays on screen before the next card loads. Speech
// is deliberately not part of this: the review flow never waits on audio, which
// can start late or not at all (no output device, a muted or backgrounded tab,
// an autoplay policy). The sentence keeps playing across the transition.
const ADVANCE_DELAY_MS = 450;

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

function clampWpm(value: number) {
  if (!Number.isFinite(value)) return BASE_WPM;
  return Math.min(MAX_WPM, Math.max(MIN_WPM, Math.round(value)));
}

function loadSpeechSettings(): SpeechSettings {
  try {
    const raw = window.localStorage.getItem(SPEECH_SETTINGS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<SpeechSettings>;
      return {
        voiceURI: typeof parsed.voiceURI === "string" ? parsed.voiceURI : "",
        rate: clampWpm(Number(parsed.rate))
      };
    }
  } catch {
    // Unreadable or malformed storage just falls back to the defaults.
  }
  return { voiceURI: "", rate: BASE_WPM };
}

function saveSpeechSettings(value: SpeechSettings) {
  try {
    window.localStorage.setItem(SPEECH_SETTINGS_KEY, JSON.stringify(value));
  } catch {
    // Storage can be unavailable (private mode, blocked site data); the setting
    // still applies for this session.
  }
}

// Chrome populates the voice list asynchronously, so read it now and again on
// every change rather than once at mount.
function useEnglishVoices() {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);

  useEffect(() => {
    const synth = window.speechSynthesis;
    if (!synth) return;
    const read = () =>
      setVoices(synth.getVoices().filter((voice) => voice.lang.toLowerCase().startsWith("en")));
    read();
    synth.addEventListener("voiceschanged", read);
    return () => synth.removeEventListener("voiceschanged", read);
  }, []);

  return voices;
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
  // undefined while the session cookie is being checked, null when signed out.
  const [user, setUser] = useState<User | null | undefined>(undefined);

  useEffect(() => {
    request<{ user: User }>("/api/auth/me")
      .then((payload) => setUser(payload.user))
      .catch(() => setUser(null));
  }, []);

  if (user === undefined) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f7f7f4]">
        <p className="text-gray-500">Loading...</p>
      </main>
    );
  }

  if (user === null) {
    return <AuthScreen onAuthenticated={setUser} />;
  }

  // Remounting per account keeps one learner's cards and stats from lingering
  // on screen after a different one signs in.
  return <Trainer key={user.id} user={user} onSignedOut={() => setUser(null)} />;
}

function Trainer({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [card, setCard] = useState<Card | null>(null);
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [settings, setSettings] = useState<Settings>({ show_sentence_translation: false });
  const [speech, setSpeech] = useState<SpeechSettings>(loadSpeechSettings);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Chrome garbage-collects an utterance that nothing references, which cuts
  // playback short. Holding the current one here keeps it alive until it is
  // replaced by the next.
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  const voices = useEnglishVoices();
  const speechSupported = typeof window !== "undefined" && "speechSynthesis" in window;

  const visibleSentence = useMemo(() => {
    if (result) {
      return highlightSentence(result.example_sentence, result.correct_answer);
    }
    return card?.cloze_sentence ?? "";
  }, [card, result]);

  // A session can expire mid-review, so every call funnels through here and a
  // 401 drops straight back to the sign-in screen.
  const guarded = useCallback(
    async (action: () => Promise<void>) => {
      try {
        await action();
      } catch (caught) {
        if (isUnauthorized(caught)) {
          onSignedOut();
          return;
        }
        setMessage(errorMessage(caught));
        setLoading(false);
      }
    },
    [onSignedOut]
  );

  function updateSpeech(next: Partial<SpeechSettings>) {
    setSpeech((current) => {
      const merged = { ...current, ...next };
      saveSpeechSettings(merged);
      return merged;
    });
  }

  // Once the browser reports its voices, adopt a sensible default if nothing is
  // stored yet or the stored voice is not installed here.
  useEffect(() => {
    if (voices.length === 0) return;
    if (voices.some((voice) => voice.voiceURI === speech.voiceURI)) return;
    updateSpeech({ voiceURI: preferredVoice(voices, "en-US") });
  }, [voices]);

  const speak = useCallback(
    (text: string) => {
      const synth = window.speechSynthesis;
      const clean = text.replace(/\s+/g, " ").trim();
      if (!synth || !clean) return;

      // Drop anything still queued so rapid answers do not stack up utterances.
      synth.cancel();

      const utterance = new SpeechSynthesisUtterance(clean);
      const voice = voices.find((item) => item.voiceURI === speech.voiceURI);
      if (voice) {
        utterance.voice = voice;
        utterance.lang = voice.lang;
      } else {
        utterance.lang = "en-US";
      }
      utterance.rate = Math.min(10, Math.max(0.1, speech.rate / BASE_WPM));
      utteranceRef.current = utterance;
      synth.speak(utterance);
    },
    [voices, speech.voiceURI, speech.rate]
  );

  async function loadStats() {
    setStats(await request<Stats>("/api/stats"));
  }

  async function loadSettings() {
    setSettings(await request<Settings>("/api/settings"));
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

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!card || submitting || result?.is_correct || (result && answer.trim() === "")) return;
    setSubmitting(true);
    await guarded(async () => {
      const review = await request<ReviewResult>("/api/review", {
        method: "POST",
        body: JSON.stringify({ word_id: card.id, user_answer: answer })
      });
      setResult(review);
      await loadStats();
      if (review.is_correct) {
        speak(review.example_sentence);
        window.setTimeout(() => {
          void guarded(loadNext);
        }, ADVANCE_DELAY_MS);
      } else {
        setAnswer("");
        window.setTimeout(() => inputRef.current?.focus(), 50);
        speak(review.example_sentence);
      }
    });
    setSubmitting(false);
  }

  function handleAnswerChange(value: string) {
    if (result && !result.is_correct && value !== "") {
      setResult(null);
    }
    setAnswer(value);
  }

  function updateSetting(next: Partial<Settings>) {
    void guarded(async () => {
      setSettings(
        await request<Settings>("/api/settings", { method: "PATCH", body: JSON.stringify(next) })
      );
    });
  }

  function signOut() {
    void guarded(async () => {
      await request<{ status: string }>("/api/auth/logout", { method: "POST" });
      onSignedOut();
    });
  }

  useEffect(() => {
    void guarded(async () => {
      await Promise.all([loadStats(), loadSettings(), loadNext()]);
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
            <p className="mt-2 flex items-center gap-2 text-sm text-gray-600">
              <span className="font-medium text-gray-900">{user.username}</span>
              <span className="text-gray-400">·</span>
              <span className="text-xs">{user.timezone}</span>
              <button
                type="button"
                onClick={signOut}
                className="ml-1 border border-gray-300 px-2 py-0.5 text-xs text-gray-700"
              >
                登出
              </button>
            </p>
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
                        <ResultLine tone="incorrect" text={result.is_blank ? "不会" : "Incorrect"} />
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
                    disabled={!speechSupported}
                    className="h-12 border border-gray-300 px-5 text-sm font-medium text-gray-900 disabled:cursor-not-allowed disabled:text-gray-400"
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
            <p className="mt-1 text-xs text-gray-500">由浏览器朗读，仅保存在本机。</p>

            {!speechSupported ? (
              <p className="mt-4 border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                当前浏览器不支持语音合成，发音功能不可用。
              </p>
            ) : (
              <div className="mt-4 space-y-4">
                <label className="block text-sm text-gray-600">
                  语音
                  <select
                    value={speech.voiceURI}
                    onChange={(event) => updateSpeech({ voiceURI: event.target.value })}
                    disabled={voices.length === 0}
                    className="mt-1 h-10 w-full border border-gray-300 bg-white px-2 text-gray-950 disabled:bg-gray-100"
                  >
                    {voices.length === 0 ? (
                      <option value="">系统未安装英文语音</option>
                    ) : (
                      voices.map((voice) => (
                        <option key={voice.voiceURI} value={voice.voiceURI}>
                          {voice.name} · {voice.lang}
                        </option>
                      ))
                    )}
                  </select>
                </label>

                <label className="block text-sm text-gray-600">
                  语速 {speech.rate}
                  <input
                    type="range"
                    min={MIN_WPM}
                    max={MAX_WPM}
                    value={speech.rate}
                    onChange={(event) => updateSpeech({ rate: clampWpm(Number(event.target.value)) })}
                    className="mt-2 w-full"
                  />
                </label>

                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => updateSpeech({ voiceURI: preferredVoice(voices, "en-US") })}
                    disabled={voices.length === 0}
                    className="h-10 border border-gray-300 text-sm disabled:text-gray-400"
                  >
                    美式
                  </button>
                  <button
                    type="button"
                    onClick={() => updateSpeech({ voiceURI: preferredVoice(voices, "en-GB") })}
                    disabled={voices.length === 0}
                    className="h-10 border border-gray-300 text-sm disabled:text-gray-400"
                  >
                    英式
                  </button>
                </div>
              </div>
            )}

            <h2 className="mt-7 text-sm font-semibold text-gray-900">账号</h2>
            <PasskeySettings onSignedOut={onSignedOut} />
            <PasswordForm />
          </aside>
        </section>
      </div>
    </main>
  );
}

function PasswordForm() {
  const [open, setOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await request<{ status: string }>("/api/auth/password", {
        method: "PATCH",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
      });
      setStatus("密码已更新，其他设备的登录已失效。");
      setCurrentPassword("");
      setNewPassword("");
    } catch (caught) {
      // Deliberately not routed through the shared 401 handler: a wrong current
      // password answers 401 too, and mistaking that for an expired session
      // would sign the user out over a typo.
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-3 h-10 w-full border border-gray-300 text-sm text-gray-700"
      >
        修改密码
      </button>
    );
  }

  return (
    <form onSubmit={submit} className="mt-3 space-y-3">
      <input
        type="password"
        value={currentPassword}
        onChange={(event) => setCurrentPassword(event.target.value)}
        placeholder="当前密码"
        autoComplete="current-password"
        className="h-10 w-full border border-gray-300 px-3 text-sm outline-none focus:border-gray-950"
      />
      <input
        type="password"
        value={newPassword}
        onChange={(event) => setNewPassword(event.target.value)}
        placeholder="新密码（至少 8 位）"
        autoComplete="new-password"
        className="h-10 w-full border border-gray-300 px-3 text-sm outline-none focus:border-gray-950"
      />
      {error ? <p className="text-xs text-red-700">{error}</p> : null}
      {status ? <p className="text-xs text-emerald-700">{status}</p> : null}
      <div className="grid grid-cols-2 gap-2">
        <button
          type="submit"
          disabled={busy || !currentPassword || !newPassword}
          className="h-10 border border-gray-950 bg-gray-950 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-300"
        >
          {busy ? "提交中" : "保存"}
        </button>
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            setError("");
            setStatus("");
          }}
          className="h-10 border border-gray-300 text-sm"
        >
          取消
        </button>
      </div>
    </form>
  );
}

function preferredVoice(voices: SpeechSynthesisVoice[], lang: "en-US" | "en-GB") {
  const target = lang.toLowerCase();
  const match =
    voices.find((voice) => voice.lang.toLowerCase().replace("_", "-") === target) ?? voices[0];
  return match?.voiceURI ?? "";
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
