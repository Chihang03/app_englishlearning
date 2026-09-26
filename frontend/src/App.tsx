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

type Page = "home" | "study" | "settings";

function pageFromHash(): Page {
  const value = window.location.hash.slice(1);
  return value === "study" || value === "settings" ? value : "home";
}

function Trainer({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [page, setPage] = useState<Page>("home");
  const [hasStarted, setHasStarted] = useState(false);
  const [card, setCard] = useState<Card | null>(null);
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [statsReady, setStatsReady] = useState(false);
  const [settings, setSettings] = useState<Settings>({ show_sentence_translation: false });
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [speech, setSpeech] = useState<SpeechSettings>(loadSpeechSettings);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");
  const [queueMessage, setQueueMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const shellRef = useRef<HTMLElement>(null);
  const questionRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDetailsElement>(null);
  const pageRef = useRef<Page>("home");
  const loadingRef = useRef(false);
  const submittingRef = useRef(false);
  const settingsSavingRef = useRef(false);
  const mountedRef = useRef(true);
  // Keeping the input mounted and the utterance referenced allows consecutive
  // answers without intentionally closing the keyboard or cutting off audio.
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);
  const voices = useEnglishVoices();
  const speechSupported = "speechSynthesis" in window;

  const sentenceParts = useMemo(() => {
    const parts = (card?.cloze_sentence ?? "").split("_______");
    // An imported sentence without a matching word can still be answered.
    return parts.length > 1 ? parts : [...parts, ""];
  }, [card]);

  function focusAnswer() {
    inputRef.current?.focus({ preventScroll: true });
    inputRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  const guarded = useCallback(
    async (action: () => Promise<void>) => {
      try {
        await action();
      } catch (caught) {
        if (!mountedRef.current) return;
        if (isUnauthorized(caught)) {
          onSignedOut();
          return;
        }
        setMessage(errorMessage(caught));
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

  useEffect(() => {
    if (voices.length === 0 || voices.some((voice) => voice.voiceURI === speech.voiceURI)) return;
    updateSpeech({ voiceURI: preferredVoice(voices, "en-US") });
  }, [voices, speech.voiceURI]);

  const speak = useCallback(
    (text: string) => {
      const synth = window.speechSynthesis;
      const clean = text.replace(/\s+/g, " ").trim();
      if (!synth || !clean) return;
      try {
        synth.cancel();
        const utterance = new SpeechSynthesisUtterance(clean);
        const voice = voices.find((item) => item.voiceURI === speech.voiceURI);
        if (voice) utterance.voice = voice;
        utterance.lang = voice?.lang ?? "en-US";
        utterance.rate = Math.min(10, Math.max(0.1, speech.rate / BASE_WPM));
        utteranceRef.current = utterance;
        synth.speak(utterance);
      } catch {
        // Speech is optional; it must not interrupt an accepted review.
        setMessage("当前浏览器暂时无法朗读，可以继续答题。");
      }
    },
    [voices, speech.voiceURI, speech.rate]
  );

  async function loadStats() {
    const payload = await request<Stats>("/api/stats");
    if (!mountedRef.current) return;
    setStats(payload);
    setStatsReady(true);
  }

  async function loadSettings() {
    const payload = await request<Settings>("/api/settings");
    if (mountedRef.current) setSettings(payload);
  }

  async function loadNext() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setMessage("");
    try {
      const payload = await request<{ card: Card | null; message?: string }>("/api/next");
      if (!mountedRef.current) return;
      // Do not remove the old input while fetching: mobile keyboards depend on
      // the focused DOM node surviving the transition to the next card.
      setCard(payload.card);
      setResult(null);
      setAnswer("");
      setQueueMessage(payload.message ?? "");
    } finally {
      loadingRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }

  async function submitAnswer(value: string) {
    if (!card || loadingRef.current || submittingRef.current || result?.is_correct) return;
    if (result && value.trim() === "") return;
    submittingRef.current = true;
    setSubmitting(true);
    setMessage("");
    await guarded(async () => {
      const review = await request<ReviewResult>("/api/review", {
        method: "POST",
        body: JSON.stringify({ word_id: card.id, user_answer: value })
      });
      if (!mountedRef.current) return;
      setResult(review);
      if (!review.is_correct) setAnswer("");
      if (pageRef.current === "study") {
        speak(review.example_sentence);
        focusAnswer();
      }
      // A failed stats refresh must never cause an accepted answer to be posted
      // again. Review feedback and advancement do not depend on this request.
      void guarded(loadStats);
    });
    submittingRef.current = false;
    if (mountedRef.current) setSubmitting(false);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void submitAnswer(answer);
  }

  function handleAnswerChange(value: string) {
    if (result && !result.is_correct && value !== "") setResult(null);
    setAnswer(value);
  }

  function navigate(next: Page) {
    if (next === pageRef.current) return;
    inputRef.current?.blur();
    if (menuRef.current) menuRef.current.open = false;
    window.speechSynthesis?.cancel();
    pageRef.current = next;
    window.history.pushState(null, "", `#${next}`);
    setPage(next);
  }

  function updateSetting(next: Partial<Settings>) {
    if (settingsSavingRef.current) return;
    settingsSavingRef.current = true;
    setSettingsSaving(true);
    const previous = settings;
    setSettings({ ...previous, ...next });
    void guarded(async () => {
      try {
        const payload = await request<Settings>("/api/settings", {
          method: "PATCH", body: JSON.stringify(next)
        });
        if (mountedRef.current) setSettings(payload);
      } catch (caught) {
        if (mountedRef.current) setSettings(previous);
        throw caught;
      } finally {
        settingsSavingRef.current = false;
        if (mountedRef.current) setSettingsSaving(false);
      }
    });
  }

  function signOut() {
    void guarded(async () => {
      await request<{ status: string }>("/api/auth/logout", { method: "POST" });
      onSignedOut();
    });
  }

  useEffect(() => {
    mountedRef.current = true;
    // Each authenticated session starts at Home, including after a reload.
    window.history.replaceState(null, "", "#home");
    const syncPage = () => {
      const next = pageFromHash();
      if (menuRef.current) menuRef.current.open = false;
      pageRef.current = next;
      setPage(next);
    };
    window.addEventListener("popstate", syncPage);
    window.addEventListener("hashchange", syncPage);
    void guarded(async () => { await Promise.all([loadStats(), loadSettings()]); });
    return () => {
      mountedRef.current = false;
      window.removeEventListener("popstate", syncPage);
      window.removeEventListener("hashchange", syncPage);
      window.speechSynthesis?.cancel();
    };
  }, []);

  useEffect(() => {
    window.scrollTo(0, 0);
    if (page === "study") {
      if (!hasStarted) {
        setHasStarted(true);
        void guarded(loadNext);
      } else {
        focusAnswer();
      }
    } else if (page === "home" && hasStarted) {
      void guarded(loadStats);
    }
  }, [page]);

  useEffect(() => {
    if (pageRef.current === "study" && card && !loading) {
      questionRef.current?.scrollTo(0, 0);
      focusAnswer();
    }
  }, [card, loading]);

  useEffect(() => {
    if (page !== "study") return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [page]);

  useEffect(() => {
    if (page !== "study" || !result?.is_correct) return;
    const timer = window.setTimeout(() => { void guarded(loadNext); }, ADVANCE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [page, result]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const updateViewport = () => {
      const height = viewport?.height ?? window.innerHeight;
      shellRef.current?.style.setProperty("--viewport-height", `${height}px`);
      shellRef.current?.style.setProperty("--viewport-top", `${viewport?.offsetTop ?? 0}px`);
      if (pageRef.current === "study") {
        window.requestAnimationFrame(() => {
          const active = document.activeElement;
          if (active instanceof HTMLInputElement && active.classList.contains("sentence-input")) {
            active.scrollIntoView({ block: "nearest", inline: "nearest" });
          }
        });
      }
    };
    updateViewport();
    viewport?.addEventListener("resize", updateViewport);
    viewport?.addEventListener("scroll", updateViewport);
    window.addEventListener("resize", updateViewport);
    return () => {
      viewport?.removeEventListener("resize", updateViewport);
      viewport?.removeEventListener("scroll", updateViewport);
      window.removeEventListener("resize", updateViewport);
    };
  }, []);

  const blankSpeech = card?.cloze_sentence.replace(/_______/g, "blank") ?? "";
  const busy = loading || submitting || Boolean(result?.is_correct);

  return (
    <main ref={shellRef} className={`trainer-shell ${page === "study" ? "is-studying" : ""}`}>
      {page === "home" ? (
        <Home user={user} stats={stats} ready={statsReady} hasStarted={hasStarted}
          onStudy={() => navigate("study")} onSettings={() => navigate("settings")}
          onRefresh={() => { setMessage(""); void guarded(loadStats); }} />
      ) : null}

      {/* Hidden rather than unmounted: returning Home preserves the current
          question and draft, and next-card requests preserve the input node. */}
      <section hidden={page !== "study"} className="study-page" aria-label="学习">
        <header className="study-header">
          <button type="button" className="icon-button" onClick={() => navigate("home")} aria-label="返回首页">
            <Icon name="back" />
          </button>
          <div className="study-progress">
            <span className="eyebrow">专注学习</span>
            <span>今日已答 {statsReady ? stats.today_learning : "—"} 次</span>
          </div>
          <details ref={menuRef} className="study-menu">
            <summary className="icon-button" aria-label="更多学习操作"><Icon name="more" /></summary>
            <div className="study-menu-panel">
              <button type="button" onClick={() => navigate("settings")}>学习设置</button>
              <button type="button" onClick={() => navigate("home")}>回到首页</button>
            </div>
          </details>
        </header>

        {card ? (
          <form onSubmit={submit} className="question-card" aria-busy={loading} aria-label="当前题目">
            <div ref={questionRef} className="question-content" tabIndex={0} aria-label="题目内容">
              <div className="question-heading">
                <span className="pill">{card.status === "New" ? "新词" : "复习"}</span>
                <button type="button" className="icon-button pronunciation-button"
                  onClick={() => speak(result ? result.example_sentence : blankSpeech)}
                  disabled={!speechSupported} aria-label="朗读英文句子" title="朗读英文句子">
                  <Icon name="sound" />
                </button>
              </div>
              <p className="english-sentence">
                {sentenceParts.flatMap((part, index) => [
                  <span key={`text-${index}`}>{part}</span>,
                  index < sentenceParts.length - 1 ? (
                    <input key={`blank-${index}`} id={index === 0 ? "study-answer" : undefined}
                      ref={index === 0 ? inputRef : undefined}
                      className={`sentence-input ${result?.is_correct ? "is-correct" : result ? "is-retry" : ""}`}
                      style={{ width: `${Math.min(18, Math.max(5, (result ? result.correct_answer.length : answer.length) + 1))}ch` }}
                      value={result?.is_correct ? result.correct_answer : answer}
                      onChange={(event) => { if (!busy) handleAnswerChange(event.target.value); }}
                      aria-label={index === 0 ? "输入英文答案" : `输入英文答案，第 ${index + 1} 处挖空`}
                      aria-busy={busy} aria-describedby="answer-feedback"
                      autoComplete="off" autoCorrect="off" autoCapitalize="none" spellCheck={false}
                      enterKeyHint="send" inputMode="text" />
                  ) : null
                ])}
              </p>
              <div className="meaning-block">
                <span className="part-of-speech">{card.part_of_speech}</span>
                <p className="word-meaning">{card.definition_cn}</p>
                {settings.show_sentence_translation && card.example_translation_cn ? (
                  <p className="sentence-translation">{card.example_translation_cn}</p>
                ) : null}
              </div>
              <div id="answer-feedback" className="answer-feedback" aria-live="polite" aria-atomic="true">
                {result?.is_correct ? <p className="feedback-correct">✓ 答对了</p> : result ? (
                  <div className="feedback-incorrect">
                    <p>{result.is_blank ? "没关系，再记一次" : "再试一次"}</p>
                    <p>正确答案：<strong>{result.correct_answer}</strong></p>
                  </div>
                ) : <p className="question-hint">回车提交；留空回车可查看答案。</p>}
              </div>
              {message ? <p role="alert" className="error-notice">{message}</p> : null}
            </div>
            <div className="study-actions">
              <span className="study-action-status" role="status">{loading ? "正在加载下一题…" : result?.is_correct ? "即将进入下一题…" : ""}</span>
              <div className="study-action-buttons">
                {result?.is_correct && !loading ? (
                  <button type="button" className="text-button" onClick={() => { void guarded(loadNext); }}>下一题 <Icon name="arrow" /></button>
                ) : null}
                <button type="submit" className="primary-button" disabled={busy}
                  onPointerDown={(event) => event.preventDefault()}>
                  {submitting ? "提交中" : loading ? "加载中" : "提交"}
                </button>
              </div>
            </div>
          </form>
        ) : (
          <div className="study-empty panel" role="status">
            <span className="empty-icon"><Icon name={loading ? "book" : message ? "more" : "check"} /></span>
            <h1>{loading ? "准备好，开始学习" : message ? "题目暂时没有加载成功" : "今天的复习已完成"}</h1>
            <p>{loading ? "正在准备你的第一道题…" : message || (queueMessage ? "目前没有到期复习或可学习的新词，稍后再来看看。" : "正在准备题目。")}</p>
            {!loading ? <div className="empty-actions">
              <button type="button" className="primary-button" onClick={() => navigate("home")}>返回首页</button>
              <button type="button" className="secondary-button" onClick={() => { void guarded(loadNext); }}>重新检查</button>
            </div> : null}
          </div>
        )}
      </section>

      {page === "settings" ? (
        <SettingsPage user={user} settings={settings} speech={speech} voices={voices}
          settingsSaving={settingsSaving}
          speechSupported={speechSupported} onBack={() => navigate("home")}
          onSetting={updateSetting} onSpeech={updateSpeech} onSignOut={signOut} onSessionExpired={onSignedOut} />
      ) : null}

      {message && page !== "study" ? <p role="alert" className="global-error error-notice">{message}</p> : null}
      {page !== "study" ? (
        <nav className="bottom-nav" aria-label="主导航">
          <button type="button" onClick={() => navigate("home")} aria-current={page === "home" ? "page" : undefined}>
            <Icon name="home" /><span>首页</span>
          </button>
          <button type="button" className="nav-study" onClick={() => navigate("study")}>
            <span className="nav-study-icon"><Icon name="arrow" /></span>
            <span>{hasStarted ? "继续学习" : "开始学习"}</span>
          </button>
          <button type="button" onClick={() => navigate("settings")} aria-current={page === "settings" ? "page" : undefined}>
            <Icon name="settings" /><span>设置</span>
          </button>
        </nav>
      ) : null}
    </main>
  );
}

function Home({ user, stats, ready, hasStarted, onStudy, onSettings, onRefresh }: {
  user: User; stats: Stats; ready: boolean; hasStarted: boolean;
  onStudy: () => void; onSettings: () => void; onRefresh: () => void;
}) {
  const value = (count: number) => ready ? count.toLocaleString() : "—";
  return (
    <section className="home-page page-container" aria-label="首页">
      <header className="page-header">
        <div><span className="eyebrow">CONTEXT · 语境学词</span><h1>你好，{user.username}</h1></div>
        <button type="button" className="icon-button header-settings" aria-label="打开设置" onClick={onSettings}><Icon name="settings" /></button>
      </header>
      <div className="home-intro"><p>每天一点，让英语更熟悉。</p><span className="streak-badge"><Icon name="spark" /> 连续学习 {value(stats.streak_days)} 天</span></div>
      <div className="home-main-grid">
        <section className="today-panel panel">
          <div className="section-heading"><h2>今日学习</h2><span className="subtle-label">{ready && stats.today_learning > 0 ? "每一次练习都算数" : "从一个句子开始"}</span></div>
          <div className="today-metrics">
            <div><strong>{value(stats.today_learning)}</strong><span>答题次数</span></div>
            <div><strong>{ready && stats.today_learning > 0 ? `${stats.today_accuracy}%` : "—"}</strong><span>今日正确率</span></div>
          </div>
          <p className="metric-note">答题次数包含复习和重试。</p>
        </section>
        <section className="learning-plan panel">
          <span className="plan-icon"><Icon name="book" /></span>
          <span className="eyebrow">在句子里记住单词</span>
          <h2>{hasStarted ? "接着上次，继续练习" : "你的下一次进步，从这里开始"}</h2>
          <p>读英文，想中文，写下答案。<br />复习安排会随你的学习进度更新。</p>
          <button type="button" className="plan-start text-button" onClick={onStudy}>{hasStarted ? "继续学习" : "开始学习"}<Icon name="arrow" /></button>
        </section>
      </div>
      <section className="overview-section">
        <div className="section-heading"><h2>学习概览</h2><button type="button" className="text-button" onClick={onRefresh} aria-label="刷新学习数据"><Icon name="refresh" /> 刷新</button></div>
        <div className="overview-grid">
          <Metric icon="book" label="累计学过" value={value(stats.total_learned)} caption="不同单词" />
          <Metric icon="refresh" label="待复习错词" value={value(stats.due_lapses)} caption="今天到期" />
          <Metric icon="spark" label="可学新词" value={value(stats.new_words)} caption="慢慢积累" />
          <Metric icon="check" label="已掌握" value={value(stats.mastered)} caption="进入间隔复习" />
        </div>
      </section>
      <details className="learning-details panel">
        <summary><span><Icon name="chart" /> 更多学习数据</span><Icon name="chevron" /></summary>
        <dl className="detail-metrics">
          <div><dt>学习中</dt><dd>{value(stats.learning)}</dd></div>
          <div><dt>学习中今日到期</dt><dd>{value(stats.learning_due)}</dd></div>
          <div><dt>有过错误的单词</dt><dd>{value(stats.lapse_words)}</dd></div>
          <div><dt>长期熟记</dt><dd>{value(stats.mature)}</dd></div>
        </dl>
      </details>
      <p className="home-footnote">学习记录跟随账号，发音偏好保存在当前设备。</p>
    </section>
  );
}

function Metric({ icon, label, value, caption }: { icon: IconName; label: string; value: string; caption: string }) {
  return <div className="metric-card panel"><span className="metric-icon"><Icon name={icon} /></span><strong>{value}</strong><span className="metric-label">{label}</span><span className="metric-caption">{caption}</span></div>;
}

function SettingsPage({ user, settings, settingsSaving, speech, voices, speechSupported, onBack, onSetting, onSpeech, onSignOut, onSessionExpired }: {
  user: User; settings: Settings; speech: SpeechSettings; voices: SpeechSynthesisVoice[];
  settingsSaving: boolean;
  speechSupported: boolean; onBack: () => void; onSetting: (next: Partial<Settings>) => void;
  onSpeech: (next: Partial<SpeechSettings>) => void; onSignOut: () => void; onSessionExpired: () => void;
}) {
  return (
    <section className="settings-page page-container" aria-label="设置">
      <header className="settings-header"><button type="button" className="icon-button" onClick={onBack} aria-label="返回首页"><Icon name="back" /></button><h1>设置</h1><span className="header-spacer" /></header>
      <div className="account-card panel"><span className="account-avatar"><Icon name="user" /></span><div><h2>{user.username}</h2><p>{user.timezone}</p></div></div>
      <section className="settings-group panel">
        <h2><Icon name="book" /> 学习显示</h2>
        <label className="setting-row"><span><strong>句子中文翻译</strong><small>在单词释义下显示完整句子的翻译</small></span><input type="checkbox" className="setting-switch" disabled={settingsSaving} checked={settings.show_sentence_translation} onChange={(event) => onSetting({ show_sentence_translation: event.target.checked })} /></label>
      </section>
      <section className="settings-group panel">
        <h2><Icon name="sound" /> 发音</h2>
        <p className="settings-hint">由当前设备的浏览器朗读，偏好仅保存在本机。</p>
        {!speechSupported ? <p className="warning-notice">当前浏览器不支持语音朗读。</p> : (
          <div className="speech-controls">
            <label className="setting-field"><span>英文语音</span><select value={speech.voiceURI} onChange={(event) => onSpeech({ voiceURI: event.target.value })} disabled={voices.length === 0}>
              {voices.length === 0 ? <option value="">暂无可用英文语音</option> : voices.map((voice) => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang}</option>)}
            </select></label>
            <div className="voice-shortcuts"><button type="button" className="secondary-button" disabled={voices.length === 0} onClick={() => onSpeech({ voiceURI: preferredVoice(voices, "en-US") })}>美式语音</button><button type="button" className="secondary-button" disabled={voices.length === 0} onClick={() => onSpeech({ voiceURI: preferredVoice(voices, "en-GB") })}>英式语音</button></div>
            <label className="setting-field"><span>语速 <strong>{speech.rate}</strong></span><input type="range" min={MIN_WPM} max={MAX_WPM} value={speech.rate} onChange={(event) => onSpeech({ rate: clampWpm(Number(event.target.value)) })} /><span className="range-labels"><small>慢</small><small>快</small></span></label>
          </div>
        )}
      </section>
      <section className="settings-group panel">
        <h2><Icon name="user" /> 账号安全</h2>
        <PasskeySettings onSignedOut={onSessionExpired} />
        <PasswordForm />
      </section>
      <button type="button" className="sign-out-button secondary-button" onClick={onSignOut}>退出登录</button>
    </section>
  );
}

type IconName = "home" | "settings" | "arrow" | "back" | "more" | "sound" | "book" | "spark" | "check" | "refresh" | "chart" | "chevron" | "user";

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, React.ReactNode> = {
    home: <><path d="m3 10 9-7 9 7" /><path d="M5 9v12h5v-7h4v7h5V9" /></>,
    settings: <><path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3h3l1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3Z" /><circle cx="12" cy="12" r="3" /></>,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6" /></>,
    back: <><path d="M20 12H4m6-6-6 6 6 6" /></>,
    more: <><circle cx="12" cy="5" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="12" cy="19" r="1" /></>,
    sound: <><path d="m11 4-6 5H2v6h3l6 5Z" /><path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14" /></>,
    book: <><path d="M12 5C9 3 5 3 2 4v16c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1Zm0 0v16" /></>,
    spark: <><path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z" /></>,
    check: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6 6a8 8 0 0 1 13 2m-1 10A8 8 0 0 1 5 16" /></>,
    chart: <><path d="M4 20V10m5 10V4m6 16v-7m5 7V7" /></>,
    chevron: <><path d="m9 5 7 7-7 7" /></>,
    user: <><circle cx="12" cy="7" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2Z" /></>
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
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
        aria-label="当前密码"
        autoComplete="current-password"
        className="h-10 w-full border border-gray-300 px-3 text-sm outline-none focus:border-gray-950"
      />
      <input
        type="password"
        value={newPassword}
        onChange={(event) => setNewPassword(event.target.value)}
        placeholder="新密码（至少 8 位）"
        aria-label="新密码（至少 8 位）"
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

export default App;
