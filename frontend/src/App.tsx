import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { AuthScreen } from "./AuthScreen";
import { AdminDashboard } from "./AdminDashboard";
import { PasskeySettings } from "./PasskeySettings";
import { StudyCard } from "./StudyCard";
import { StudyDeck } from "./StudyDeck";
import type { PreviousQuestion } from "./StudyDeck";
import { StudyTools } from "./StudyTools";
import { MutedWords } from "./MutedWords";
import { LearningCalendar } from "./LearningCalendar";
import { useLearningReminder } from "./learningReminder";
import { BASE_WPM, SPEECH_SPEEDS, useSpeechSettings } from "./speechSettings";
import type { StudyTool } from "./StudyTools";
import { ApiError, errorMessage, hasPendingWrites, isUnauthorized, onApiWrite, request } from "./api";
import { version } from "./version.json";
import { ReadCache } from "./readCache";
import { StudyContentCache } from "./studyContentCache";
import { MeaningExposureQueue } from "./meaningExposure";
import { formatStudyTime } from "./studyTime";
import { activateAppUpdate } from "./appWorker";
import { OfflineLibrary } from "./OfflineLibrary";
import { rememberLearner, savedLearner } from "./offlineSession";
import { canReloadForUpdate, useForegroundRefresh, useVersionUpdate } from "./lifecycle";
import type { Card, ReviewResult, Settings, SpeechSettings, Stats, User, WordList } from "./types";

// If a browser never starts speech, retain the answer with a manual next-card
// action. Once speech starts, only its end event allows automatic advancement.
const SPEECH_START_TIMEOUT_MS = 8000;

const emptyStats: Stats = {
  total_study_time_ms: 0,
  today_learning: 0,
  today_completed_cards: 0,
  today_accuracy: 0,
  today_success: 0,
  today_success_senses: 0,
  today_independent_accuracy: null,
  pending_relearning: 0,
  pending_relearning_senses: 0,
  next_relearning_at: null,
  total_learned: 0,
  due_review: 0,
  new_words: 0,
  learning: 0,
  learning_due: 0,
  lapse_words: 0,
  due_lapses: 0,
  mastered: 0,
  mature: 0,
  streak_days: 0,
  learned_senses: 0,
  new_senses: 0,
  due_senses: 0,
  due_words: 0,
  mastered_senses: 0,
  legacy_unmapped_words: 0
};

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
  const [connectionError, setConnectionError] = useState(false);
  const authChecking = useRef(false);
  const signedOut = useCallback(() => { rememberLearner(null); setUser(null); }, []);
  const { available: updateAvailable, check: checkVersion } = useVersionUpdate();

  const checkSession = useCallback(async () => {
    if (authChecking.current) return;
    if (!navigator.onLine) { setConnectionError(true); return; }
    authChecking.current = true;
    setConnectionError(false);
    try {
      const payload = await request<{ user: User }>("/api/auth/me");
      setUser(payload.user);
    } catch (caught) {
      if (isUnauthorized(caught)) { rememberLearner(null); setUser(null); }
      else setConnectionError(true);
    } finally {
      authChecking.current = false;
    }
  }, []);

  useEffect(() => {
    void checkSession();
  }, []);
  useEffect(() => { if (user !== undefined) rememberLearner(user); }, [user]);
  useForegroundRefresh(() => { if (user === undefined) void checkSession(); });

  useEffect(() => {
    const markNotification = (event: MessageEvent) => {
      if (event.data?.type === "OPEN_STUDY" && user == null) {
        window.history.replaceState(null, "", "/?notification=study#study");
      }
    };
    navigator.serviceWorker?.addEventListener("message", markNotification);
    return () => navigator.serviceWorker?.removeEventListener("message", markNotification);
  }, [user]);

  if (user === undefined) {
    const cached = connectionError ? savedLearner() : null;
    if (cached) return <OfflineLibrary key={cached.id} user={cached} onRetry={() => { void checkSession(); }} />;
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-[#f7f7f4]">
        {connectionError ? <>
          <p role="alert">暂时无法连接服务器</p>
          <button type="button" className="secondary-button" onClick={() => { void checkSession(); }}>重试</button>
        </> : <p className="text-gray-500">Loading...</p>}
      </main>
    );
  }

  if (user === null) {
    return <AuthScreen onAuthenticated={setUser} />;
  }

  if (user.role === "admin") {
    return <AdminDashboard key={user.id} user={user} onSignedOut={signedOut} onSessionChanged={setUser}
      updateAvailable={updateAvailable}
      accountSecurity={<><PasskeySettings onSignedOut={signedOut} /><PasswordForm /></>} />;
  }

  // Remounting per account keeps one learner's cards and stats from lingering
  // on screen after a different one signs in.
  return <Trainer key={user.id} user={user} onSignedOut={signedOut}
    onSessionChanged={setUser} updateAvailable={updateAvailable} checkVersion={checkVersion} />;
}

type Page = "home" | "study" | "settings" | "learning-calendar" | "muted-words" | `word-list/${string}`;

function pageFromHash(): Page {
  const value = window.location.hash.slice(1);
  return value === "study" || value === "settings" || value === "learning-calendar" || value === "muted-words" || value.startsWith("word-list/") ? value as Page : "home";
}

function Trainer({ user, onSignedOut, onSessionChanged, updateAvailable, checkVersion }: {
  user: User; onSignedOut: () => void; onSessionChanged: (user: User) => void; updateAvailable: boolean;
  checkVersion: () => Promise<void>;
}) {
  const readCache = useMemo(() => new ReadCache(request, user.id), [user.id]);
  const enableLearningReminder = useLearningReminder(user.id);
  const openedFromNotification = useRef(new URLSearchParams(window.location.search).get("notification") === "study");
  const studyContentCache = useMemo(() => new StudyContentCache(user.id), [user.id]);
  const meaningExposureQueue = useMemo(() => new MeaningExposureQueue(user.id), [user.id]);
  const [resumeRequested, setResumeRequested] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const refreshingRef = useRef(false);
  const [sessionEnding, setSessionEnding] = useState(false);
  const [writeEpoch, setWriteEpoch] = useState(0);
  const [page, setPage] = useState<Page>("home");
  const [hasStarted, setHasStarted] = useState(false);
  const [card, setCard] = useState<Card | null>(null);
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [previousQuestion, setPreviousQuestion] = useState<PreviousQuestion | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [reviewMessage, setReviewMessage] = useState("");
  const reviewingRef = useRef(false);
  const deckMovingRef = useRef(false);
  const [stats, setStats] = useState<Stats>(emptyStats);
  const [statsReady, setStatsReady] = useState(false);
  const [settings, setSettings] = useState<Settings>({ show_sentence_translation: false, skip_basic_600: false, selected_word_list_ids: [], speech_rate: null });
  const currentQuestionRef = useRef({ card, result, showTranslation: settings.show_sentence_translation });
  currentQuestionRef.current = { card, result, showTranslation: settings.show_sentence_translation };
  const [wordLists, setWordLists] = useState<WordList[]>([]);
  const [wordListsReady, setWordListsReady] = useState(false);
  const studyToolsOpenRef = useRef(false);
  const [studyTool, setStudyTool] = useState<{ tool: StudyTool; card: Card; showTranslation: boolean } | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const { speech, updateSpeech, syncSpeech, applySpeechSettings } = useSpeechSettings(user.id, onSignedOut,
    () => readCache.get<Settings>("/api/settings", 30000));
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [readingCorrectAnswer, setReadingCorrectAnswer] = useState(false);
  const [message, setMessage] = useState("");
  const [queueMessage, setQueueMessage] = useState("");
  const [lastMutedWord, setLastMutedWord] = useState<string | null>(null);
  const [mutePending, setMutePending] = useState(false);
  const mutePendingRef = useRef(false);
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const shellRef = useRef<HTMLElement>(null);
  const questionRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDetailsElement>(null);
  const pageRef = useRef<Page>("home");
  const loadingRef = useRef(false);
  const submittingRef = useRef(false);
  const hintPendingRef = useRef(false);
  const [hintPending, setHintPending] = useState(false);
  const activeTimeRef = useRef(0);
  const settingsSavingRef = useRef(false);
  const mountedRef = useRef(true);
  // Keeping the input mounted and the utterance referenced allows consecutive
  // answers without intentionally closing the keyboard or cutting off audio.
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);
  const cancelSpeechRef = useRef<(() => void) | null>(null);
  const voices = useEnglishVoices();
  const speechSupported = "speechSynthesis" in window;

  useEffect(() => {
    activeTimeRef.current = 0;
    if (!card || page !== "study" || result) return;
    let previous = performance.now();
    let lastActivity = previous;
    const activity = () => { lastActivity = performance.now(); };
    const timer = window.setInterval(() => {
      const now = performance.now();
      const delta = now - previous;
      if (!document.hidden && document.hasFocus() && !studyToolsOpenRef.current && !reviewingRef.current && !deckMovingRef.current && now - lastActivity < 30000 && delta < 2000) {
        activeTimeRef.current = Math.min(300000,activeTimeRef.current + delta);
      }
      previous = now;
    }, 250);
    window.addEventListener("keydown",activity);
    window.addEventListener("pointerdown",activity);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("keydown",activity);
      window.removeEventListener("pointerdown",activity);
    };
  }, [card?.attempt_id, page, result]);

  useEffect(() => {
    if (retryAt === null || page !== "study" || reviewing) return;
    // Normally new words fill the gap. If a selected catalog is exhausted,
    // retry silently when a pending word becomes available, without a countdown.
    const timer = window.setTimeout(() => {
      setRetryAt(null);
      void guarded(loadNext);
    }, Math.max(0, retryAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [retryAt, page, reviewing]);

  function focusAnswer() {
    if (reviewingRef.current || deckMovingRef.current) return;
    inputRef.current?.focus({ preventScroll: true });
    inputRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (result && !result.is_correct) inputRef.current?.select();
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
        if (pageRef.current === "study" && reviewingRef.current) setReviewMessage(errorMessage(caught));
        else setMessage(errorMessage(caught));
      }
    },
    [onSignedOut]
  );

  useEffect(() => {
    if (!stats.next_relearning_at) return;
    // Refresh due counts when the interval ends, without showing a countdown.
    const dueAt = Date.parse(stats.next_relearning_at);
    if (!Number.isFinite(dueAt)) return;
    const timer = window.setTimeout(() => {
      void guarded(() => loadStats(true));
    }, Math.max(0, dueAt - Date.now()) + 250);
    return () => window.clearTimeout(timer);
  }, [stats.next_relearning_at, guarded]);

  useEffect(() => {
    if (voices.length === 0 || voices.some((voice) => voice.voiceURI === speech.voiceURI)) return;
    updateSpeech({ voiceURI: preferredVoice(voices, "en-US") });
  }, [voices, speech.voiceURI]);

  const speak = useCallback(
    (text: string, onEnd?: () => void, onFailure?: () => void) => {
      cancelSpeechRef.current?.();
      setReadingCorrectAnswer(Boolean(onEnd));
      const synth = window.speechSynthesis;
      const clean = text.replace(/\s+/g, " ").trim();
      const failed = () => {
        if (mountedRef.current) setReadingCorrectAnswer(false);
        if (onFailure) onFailure();
        else if (mountedRef.current) setMessage("当前浏览器暂时无法朗读，可以继续答题。");
      };
      if (!synth || !clean) {
        failed();
        return () => {};
      }

      let active = true;
      let utterance: SpeechSynthesisUtterance | null = null;
      let startTimer: number | undefined;
      const detach = () => {
        window.clearTimeout(startTimer);
        if (utterance) {
          utterance.onstart = null;
          utterance.onend = null;
          utterance.onerror = null;
        }
        if (utteranceRef.current === utterance) utteranceRef.current = null;
        if (cancelSpeechRef.current === cancel) cancelSpeechRef.current = null;
      };
      const cancel = () => {
        if (!active) return;
        active = false;
        if (mountedRef.current) setReadingCorrectAnswer(false);
        detach();
        // Detach handlers first: a cancellation is not a completed sentence.
        try { synth.cancel(); } catch { /* The browser may already be closing. */ }
      };
      const finish = (completed: boolean) => {
        if (!active) return;
        if (completed) {
          active = false;
          if (mountedRef.current) setReadingCorrectAnswer(false);
          detach();
          onEnd?.();
        } else {
          cancel();
          failed();
        }
      };
      try {
        synth.cancel();
        utterance = new SpeechSynthesisUtterance(clean);
        const voice = voices.find((item) => item.voiceURI === speech.voiceURI);
        if (voice) utterance.voice = voice;
        utterance.lang = voice?.lang ?? "en-US";
        utterance.rate = Math.min(10, Math.max(0.1, speech.rate / BASE_WPM));
        utterance.onstart = () => { window.clearTimeout(startTimer); };
        utterance.onend = () => finish(true);
        utterance.onerror = () => finish(false);
        utteranceRef.current = utterance;
        cancelSpeechRef.current = cancel;
        startTimer = window.setTimeout(() => finish(false), SPEECH_START_TIMEOUT_MS);
        synth.speak(utterance);
      } catch {
        finish(false);
      }
      return cancel;
    },
    [voices, speech.voiceURI, speech.rate]
  );

  function playSentence(text: string, advanceAfterReading: boolean) {
    setMessage("");
    speak(text, advanceAfterReading ? () => {
      if (!mountedRef.current || pageRef.current !== "study" || reviewingRef.current || deckMovingRef.current || loadingRef.current || studyToolsOpenRef.current || menuRef.current?.open) return;
      void guarded(loadNext);
    } : undefined, advanceAfterReading ? () => {
      if (!mountedRef.current || pageRef.current !== "study") return;
      setMessage("整句朗读未能完成，请按回车重播，或在右上角菜单中进入下一题。");
    } : undefined);
  }

  async function loadStats(force = false) {
    await readCache.load<Stats>("/api/stats", 10000, (payload) => {
      if (!mountedRef.current) return;
      setStats(payload);
      setStatsReady(true);
    }, force);
  }

  async function loadSettings(force = false) {
    await readCache.load<Settings>("/api/settings", 30000, (payload) => {
      if (mountedRef.current && !settingsSavingRef.current) {
        setSettings(payload);
        applySpeechSettings(payload);
      }
    }, force);
  }

  async function loadWordLists(force = false) {
    await readCache.load<{ lists: WordList[] }>("/api/word-lists", 30000, (payload) => {
      if (mountedRef.current) {
        setWordLists(payload.lists);
        setWordListsReady(true);
      }
    }, force);
  }

  async function loadNext(preserveDraft = false) {
    if (loadingRef.current || mutePendingRef.current || reviewingRef.current) return;
    const old = currentQuestionRef.current;
    cancelSpeechRef.current?.();
    setReadingCorrectAnswer(false);
    loadingRef.current = true;
    setLoading(true);
    setMessage("");
    try {
      await meaningExposureQueue.flush();
      const payload = await request<{ card: Card | null; message?: string; retry_after_seconds?: number }>("/api/next");
      if (!mountedRef.current) return;
      // Changing a filter may keep an unrelated round. Preserve its answer,
      // hints and result while updating the server's remaining count.
      if (preserveDraft && old.card && old.card.attempt_id === payload.card?.attempt_id) {
        setCard(payload.card);
        return;
      }
      cancelSpeechRef.current?.();
      setReadingCorrectAnswer(false);
      // Do not remove the old input while fetching: mobile keyboards depend on
      // the focused DOM node surviving the transition to the next card.
      if (old.card && old.result?.is_correct && old.card.attempt_id !== payload.card?.attempt_id) {
        setPreviousQuestion({ card: old.card, result: old.result, showTranslation: old.showTranslation });
      }
      closeStudyTool();
      setCard(payload.card);
      setResult(null);
      studyToolsOpenRef.current = false;
      // A resumed failed round still needs correction; it is never a clean test.
      setAnswer(payload.card?.needs_correction ? payload.card.answer_form : "");
      setQueueMessage(payload.message ?? "");
      const delay = payload.retry_after_seconds ?? 0;
      setRetryAt(delay > 0 ? Date.now() + delay * 1000 : null);
    } finally {
      loadingRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }

  async function submitAnswer(value: string, readSentence = false) {
    if (!card || reviewingRef.current || deckMovingRef.current || loadingRef.current || submittingRef.current || hintPendingRef.current || mutePendingRef.current || result?.is_correct) return;
    if (result && value.trim() === "") return;
    submittingRef.current = true;
    setSubmitting(true);
    setMessage("");
    await guarded(async () => {
      let review: ReviewResult;
      try {
        await meaningExposureQueue.flush(card.attempt_id);
        readCache.invalidate(["/api/stats"]);
        review = await request<ReviewResult>("/api/review", {
          method: "POST",
          body: JSON.stringify({ word_id: card.id, sense_id: card.sense_id, learning_unit_id: card.learning_unit_id, example_id: card.example_id, user_answer: value, attempt_id: card.attempt_id, active_response_ms: Math.round(activeTimeRef.current) })
        });
      } catch (caught) {
        // Another device or a lost response may already have completed this round.
        if (caught instanceof ApiError && caught.status === 409) {
          await loadNext();
          void guarded(loadStats);
          return;
        }
        throw caught;
      }
      if (!mountedRef.current) return;
      setResult(review);
      if (!review.is_correct) setCard({ ...card, needs_correction: true });
      if (!review.is_correct) setAnswer("");
      if (pageRef.current === "study") {
        if (readSentence) playSentence(review.example_sentence, review.is_correct);
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
    handleEnter();
  }

  function handleEnter() {
    if (!card || pageRef.current !== "study" || reviewingRef.current || deckMovingRef.current) return;
    if (submittingRef.current || loadingRef.current || hintPendingRef.current || mutePendingRef.current) {
      return;
    } else if (result) {
      // Replaying feedback must not create another review-history entry.
      playSentence(result.example_sentence, result.is_correct);
    } else {
      void submitAnswer(answer, true);
    }
  }

  async function playWordHint() {
    if (!card || reviewingRef.current || deckMovingRef.current || submittingRef.current || loadingRef.current || hintPendingRef.current || mutePendingRef.current) return;
    if (result || card.needs_correction) {
      speak(result?.correct_answer ?? card.word);
      return;
    }
    hintPendingRef.current = true;
    setHintPending(true);
    try {
      await guarded(async () => {
        // Persist before revealing the sound, including across refresh/devices.
        await request("/api/study/hint", { method: "POST", body: JSON.stringify({ attempt_id: card.attempt_id, kind: "pronunciation" }) });
        if (!mountedRef.current) return;
        setCard((current) => current?.attempt_id === card.attempt_id ? { ...current, pronunciation_used: true } : current);
        speak(card.word);
      });
    } finally {
      hintPendingRef.current = false;
      if (mountedRef.current) setHintPending(false);
    }
  }

  function openStudyTool(tool: StudyTool) {
    const target = reviewingRef.current ? previousQuestion?.card : card;
    if (!target || deckMovingRef.current || loading || submitting || hintPending || mutePending) return;
    studyToolsOpenRef.current = true;
    if (menuRef.current) menuRef.current.open = false;
    cancelSpeechRef.current?.();
    inputRef.current?.blur();
    setStudyTool({ tool, card: target, showTranslation: reviewingRef.current ? previousQuestion!.showTranslation : settings.show_sentence_translation });
  }

  function closeStudyTool() {
    const wasOpen = studyToolsOpenRef.current;
    studyToolsOpenRef.current = false;
    setStudyTool(null);
    if (wasOpen) menuRef.current?.querySelector("summary")?.focus();
  }

  async function muteVisibleWord() {
    const wasReviewing = reviewingRef.current;
    const target = wasReviewing ? previousQuestion?.card : card;
    if (!target || deckMovingRef.current || loadingRef.current || submittingRef.current || hintPendingRef.current || mutePendingRef.current) return;
    mutePendingRef.current = true;
    setMutePending(true);
    if (wasReviewing) setReviewMessage("");
    else setMessage("");
    if (menuRef.current) menuRef.current.open = false;
    cancelSpeechRef.current?.();
    let advance = false;
    readCache.invalidate(["/api/stats", "/api/word-lists", "/api/muted-words"]);
    try {
      const payload = await request<{ word: string }>(`/api/words/${target.id}/mute`, { method: "POST" });
      if (!mountedRef.current) return;
      setLastMutedWord(payload.word);
      // Muting applies to every sense and duplicate of this word. An unrelated
      // live question and its draft survive actions on the previous card.
      const live = currentQuestionRef.current.card;
      advance = !wasReviewing || live?.word.trim().toLowerCase() === payload.word.trim().toLowerCase();
      if (advance) {
        if (wasReviewing) changeReview(false);
        setCard(null);
        setResult(null);
        setAnswer("");
        currentQuestionRef.current = { ...currentQuestionRef.current, card: null, result: null };
      }
    } finally {
      mutePendingRef.current = false;
      if (mountedRef.current) setMutePending(false);
    }
    if (advance) await loadNext();
    void guarded(async () => { await Promise.all([loadStats(), loadWordLists()]); });
  }

  async function restoreMutedWord(word: string) {
    if (mutePendingRef.current) throw new Error("请等待当前操作完成。");
    mutePendingRef.current = true;
    setMutePending(true);
    readCache.invalidate(["/api/stats", "/api/word-lists", "/api/muted-words"]);
    try {
      await request(`/api/muted-words/${encodeURIComponent(word)}`, { method: "DELETE" });
      if (!mountedRef.current) return;
      setLastMutedWord((current) => current === word ? null : current);
      void guarded(async () => { await Promise.all([loadStats(), loadWordLists()]); });
    } finally {
      mutePendingRef.current = false;
      if (mountedRef.current) setMutePending(false);
    }
    // Keep an existing answer draft; resume automatically if the queue was empty.
    if (mountedRef.current && !card && pageRef.current === "study") await loadNext();
  }

  function handleAnswerChange(value: string) {
    if (result && !result.is_correct) {
      setResult(null);
      studyToolsOpenRef.current = false;
    }
    setAnswer(value);
  }

  function markAnswerExposed(source: Card, exposedSenseIds: number[]) {
    setCard((current) => {
      if (!current) return current;
      const sameAttempt = current.attempt_id === source.attempt_id;
      const relatedSense = exposedSenseIds.includes(current.sense_id);
      return sameAttempt || relatedSense ? { ...current, answer_exposed: true } : current;
    });
  }

  function navigate(next: Page) {
    if (next === pageRef.current) return;
    closeStudyTool();
    if (next !== "study") inputRef.current?.blur();
    if (menuRef.current) menuRef.current.open = false;
    cancelSpeechRef.current?.();
    pageRef.current = next;
    window.history.pushState(null, "", `#${next}`);
    if (next === "study") {
      // Mobile browsers need an editable, visible field focused within the
      // click itself. Keep that field mounted while the first card is fetched.
      flushSync(() => {
        reviewingRef.current = false;
        setReviewing(false);
        setReviewMessage("");
        if (!card) setLoading(true);
        setPage(next);
      });
      focusAnswer();
    } else {
      setPage(next);
    }
  }

  function retryStudy() {
    flushSync(() => setLoading(true));
    focusAnswer();
    void guarded(loadNext);
  }

  function changeReview(next: boolean) {
    cancelSpeechRef.current?.();
    if (menuRef.current) menuRef.current.open = false;
    reviewingRef.current = next;
    setReviewing(next);
    setReviewMessage("");
  }

  function readPreviousQuestion() {
    if (!previousQuestion || !reviewingRef.current || pageRef.current !== "study") return;
    setReviewMessage("");
    speak(previousQuestion.result.example_sentence, undefined, () => {
      if (reviewingRef.current && pageRef.current === "study") setReviewMessage("上一题朗读未能完成，请点击朗读按钮重播。");
    });
  }

  function deckMotionChanged(moving: boolean) {
    deckMovingRef.current = moving;
    if (!moving && !reviewingRef.current && pageRef.current === "study") focusAnswer();
  }

  function updateSetting(next: Partial<Settings>) {
    if (settingsSavingRef.current) return;
    settingsSavingRef.current = true;
    setSettingsSaving(true);
    const previous = settings;
    setSettings({ ...previous, ...next });
    readCache.invalidateWrite({ path: "/api/settings", method: "PATCH", body: JSON.stringify(next) });
    void guarded(async () => {
      try {
        const payload = await request<Settings>("/api/settings", {
          method: "PATCH", body: JSON.stringify(next)
        });
        if (mountedRef.current) setSettings(payload);
        if ("selected_word_list_ids" in next) void guarded(loadStats);
      } catch (caught) {
        if (mountedRef.current) setSettings(previous);
        throw caught;
      } finally {
        settingsSavingRef.current = false;
        if (mountedRef.current) setSettingsSaving(false);
      }
      if ("skip_basic_600" in next && mountedRef.current) {
        changeReview(false);
        if (hasStarted) await loadNext(true);
        await loadStats();
      }
    });
  }

  function toggleWordList(listId: string, selected: boolean) {
    const next = new Set(settings.selected_word_list_ids);
    if (selected) next.add(listId);
    else next.delete(listId);
    updateSetting({ selected_word_list_ids: [...next] });
  }

  function signOut() {
    if (sessionEnding) return;
    setSessionEnding(true);
    void guarded(async () => {
      try {
        await request<{ status: string }>("/api/auth/logout", { method: "POST" });
        readCache.clear();
        onSignedOut();
      } finally {
        if (mountedRef.current) setSessionEnding(false);
      }
    });
  }

  useEffect(() => {
    mountedRef.current = true;
    const fromNotification = openedFromNotification.current;
    window.history.replaceState(null, "", fromNotification ? "/#study" : "#home");
    const syncPage = () => {
      closeStudyTool();
      const next = pageFromHash();
      if (next !== pageRef.current) cancelSpeechRef.current?.();
      if (menuRef.current) menuRef.current.open = false;
      pageRef.current = next;
      setPage(next);
    };
    window.addEventListener("popstate", syncPage);
    window.addEventListener("hashchange", syncPage);
    const openStudy = (event: MessageEvent) => {
      if (event.data?.type === "OPEN_STUDY") navigate("study");
    };
    navigator.serviceWorker?.addEventListener("message", openStudy);
    if (fromNotification) syncPage();
    const unsubscribe = onApiWrite((write) => {
      readCache.invalidateWrite(write);
      setWriteEpoch((value) => value + 1);
      // Speech changes also save settings outside this component. Revalidate
      // the invalidated snapshot without trusting an out-of-order PATCH body.
      if (write.path === "/api/settings") void loadSettings().catch((error) => {
        if (isUnauthorized(error)) onSignedOut();
      });
    });
    void meaningExposureQueue.flush().catch(() => {});
    void studyContentCache.syncCatalog(version).catch((error) => {
      if (isUnauthorized(error)) onSignedOut();
    });
    void guarded(async () => { await Promise.all([loadStats(), loadSettings(), loadWordLists()]); });
    return () => {
      mountedRef.current = false;
      unsubscribe();
      window.removeEventListener("popstate", syncPage);
      window.removeEventListener("hashchange", syncPage);
      navigator.serviceWorker?.removeEventListener("message", openStudy);
      cancelSpeechRef.current?.();
    };
  }, []);

  useForegroundRefresh(() => setResumeRequested(true));
  async function refreshData(force = false) {
    if (refreshingRef.current || sessionEnding || submittingRef.current || loadingRef.current || settingsSavingRef.current || mutePendingRef.current || hintPendingRef.current) return;
    refreshingRef.current = true;
    setRefreshing(true);
    if (force) setMessage("");
    let syncCatalogInBackground = false;
    try {
      const session = await request<{ user: User }>("/api/auth/me");
      if (!mountedRef.current) return;
      if (session.user.id !== user.id || session.user.role !== user.role) {
        onSessionChanged(session.user);
        return;
      }
      if (force) await syncSpeech();
      if (force) await meaningExposureQueue.flush();
      if (!mountedRef.current) return;
      if (force) {
        await studyContentCache.clear();
        syncCatalogInBackground = true;
      } else {
        void studyContentCache.syncCatalog(version).catch(() => {});
      }
      // Finish all reads before allowing a version reload, even if one fails.
      const downloads = await Promise.allSettled([loadStats(true), loadSettings(true), loadWordLists(true)]);
      const failed = downloads.find((download) => download.status === "rejected" && isUnauthorized(download.reason))
        ?? downloads.find((download) => download.status === "rejected");
      if (failed?.status === "rejected") throw failed.reason;
      if (force && mountedRef.current) await checkVersion();
    } finally {
      refreshingRef.current = false;
      if (mountedRef.current) setRefreshing(false);
      // The full dictionary is a durable background download, not part of the
      // overview refresh that controls the button's busy state.
      if (syncCatalogInBackground && mountedRef.current) {
        void studyContentCache.syncCatalog(version, true).catch((error) => {
          if (isUnauthorized(error)) onSignedOut();
        });
      }
    }
  }
  useEffect(() => {
    if (!resumeRequested || refreshing || sessionEnding || submitting || loading || settingsSaving || mutePending || hintPending) return;
    setResumeRequested(false);
    void guarded(() => refreshData());
  }, [resumeRequested, refreshing, sessionEnding, submitting, loading, settingsSaving, mutePending, hintPending]);

  useEffect(() => {
    const draft = Boolean(card && !result?.is_correct && answer.length > 0);
    let active = true;
    if (updateAvailable && canReloadForUpdate({ page, draft,
      busy: hasPendingWrites() || refreshing || sessionEnding || submitting || loading || settingsSaving || mutePending || hintPending || resumeRequested || Boolean(studyTool) })) {
      void activateAppUpdate().then(() => {
        if (active && navigator.onLine && !document.hidden && !hasPendingWrites()) window.location.reload();
      }).catch(() => {});
    }
    return () => { active = false; };
  }, [updateAvailable, page, card, result, answer, refreshing, sessionEnding, submitting, loading, settingsSaving, mutePending, hintPending, resumeRequested, studyTool, writeEpoch]);

  useEffect(() => {
    window.scrollTo(0, 0);
    if (page === "settings" || page.startsWith("word-list/")) void guarded(loadWordLists);
    if (page === "study") {
      if (!hasStarted || !card) {
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
    if (pageRef.current === "study" && result && !result.is_correct) focusAnswer();
  }, [result]);

  useEffect(() => {
    if (page !== "study" && page !== "muted-words") return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [page]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const updateViewport = () => {
      const height = viewport?.height ?? window.innerHeight;
      shellRef.current?.style.setProperty("--viewport-height", `${height}px`);
      shellRef.current?.style.setProperty("--viewport-top", `${viewport?.offsetTop ?? 0}px`);
      if (pageRef.current === "study") {
        window.requestAnimationFrame(() => {
          const active = document.activeElement;
          if (active instanceof HTMLTextAreaElement && active.classList.contains("sentence-input")) {
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

  const busy = loading || submitting || hintPending || mutePending || Boolean(result?.is_correct);
  const visibleCard = reviewing ? previousQuestion?.card : card;
  const isWordListPage = page.startsWith("word-list/");
  const currentWordList = wordLists.find((list) => page === `word-list/${encodeURIComponent(list.list_id)}`);

  return (
    <main ref={shellRef} className={`trainer-shell ${page === "study" ? "is-studying" : page === "muted-words" ? "is-managing-words" : ""}`}>
      {page === "home" ? (
        <Home user={user} stats={stats} ready={statsReady} refreshing={refreshing}
          onOpenCalendar={() => navigate("learning-calendar")}
          onRefresh={() => { void guarded(() => refreshData(true)); }} />
      ) : null}

      {/* Hidden rather than unmounted: returning Home preserves the current
          question and draft, and next-card requests preserve the input node. */}
      <section hidden={page !== "study"} className="study-page" aria-label="学习">
        <header className="study-header">
          <button type="button" className="icon-button" onClick={() => navigate("home")} aria-label="返回首页">
            <Icon name="back" />
          </button>
          <div className="study-progress">
            <span>今日完成 {statsReady ? stats.today_completed_cards : "—"} 张</span>
          </div>
          <details ref={menuRef} className="study-menu">
            <summary className="icon-button" aria-label="更多学习操作"><Icon name="more" /></summary>
            <div className="study-menu-panel">
              <button type="button" disabled={!visibleCard || loading || submitting || hintPending || mutePending} onClick={() => openStudyTool("meanings")}>更多词义</button>
              <button type="button" disabled={!visibleCard || loading || submitting || hintPending || mutePending} onClick={() => openStudyTool("report")}>报告错误</button>
              <button type="button" disabled={!visibleCard || loading || submitting || hintPending || mutePending} onClick={() => { void guarded(muteVisibleWord); }}>
                不再学习此单词
              </button>
              {result?.is_correct && !reviewing ? <button type="button" disabled={loading || mutePending} onClick={() => {
                if (menuRef.current) menuRef.current.open = false;
                void guarded(loadNext);
              }}>下一题</button> : null}
            </div>
          </details>
        </header>

        {lastMutedWord ? <div className="mute-notice" role="status">
          <span>已停止学习 {lastMutedWord}</span>
          <button type="button" disabled={loading || submitting || hintPending || mutePending} onClick={() => { void guarded(() => restoreMutedWord(lastMutedWord)); }}>撤销</button>
          <button type="button" aria-label="关闭提示" onClick={() => setLastMutedWord(null)}>×</button>
        </div> : null}

        <StudyDeck currentId={card?.attempt_id ?? null} previous={previousQuestion} reviewing={reviewing}
          blocked={loading || submitting || hintPending || mutePending || Boolean(studyTool)} canAdvance={Boolean(card && result?.is_correct)}
          previousContent={previousQuestion ? <StudyCard key={previousQuestion.card.attempt_id} card={previousQuestion.card} result={previousQuestion.result}
            answer={previousQuestion.result.correct_answer} showTranslation={previousQuestion.showTranslation} readOnly
            soundIcon={<Icon name="sound" />} message={reviewMessage} wordHintDisabled={!speechSupported}
            onWordHint={readPreviousQuestion} /> : null}
          onReviewChange={changeReview} onReviewReady={readPreviousQuestion}
          onAdvance={() => { void guarded(loadNext); }} onMotionChange={deckMotionChanged}
          onInteraction={() => { cancelSpeechRef.current?.(); }}>
        {card || loading ? (
          <StudyCard card={card} result={result} answer={answer} showTranslation={settings.show_sentence_translation}
            soundIcon={<Icon name="sound" />} message={message} loading={loading} submitting={submitting}
            readingCorrectAnswer={readingCorrectAnswer} busy={busy || !card}
            wordHintDisabled={!card || !speechSupported || hintPending || loading || submitting || mutePending}
            inputRef={inputRef} questionRef={questionRef} onSubmit={submit} onEnter={handleEnter}
            onWordHint={() => { void playWordHint(); }} onAnswerChange={handleAnswerChange} />
        ) : (
          <div className="study-empty panel" role="status">
            <span className="empty-icon"><Icon name={loading ? "book" : message ? "more" : "check"} /></span>
            <h1>{loading ? "加载中…" : message ? "加载失败" : retryAt !== null ? "暂无题目" : "今日复习完成"}</h1>
            {!loading && (message || queueMessage) ? <p>{message || queueMessage}</p> : null}
            {!loading ? <div className="empty-actions">
              <button type="button" className="primary-button" onClick={() => navigate("home")}>返回首页</button>
              <button type="button" className="secondary-button" onClick={retryStudy}>重新检查</button>
            </div> : null}
          </div>
        )}
        </StudyDeck>
        {page === "study" && studyTool ? <StudyTools key={`${studyTool.card.attempt_id}-${studyTool.tool}`}
          tool={studyTool.tool} card={studyTool.card} showTranslation={studyTool.showTranslation}
          contentCache={studyContentCache} exposureQueue={meaningExposureQueue}
          onClose={closeStudyTool} onSignedOut={onSignedOut}
          onAnswerExposed={(senseIds) => markAnswerExposed(studyTool.card, senseIds)} /> : null}
      </section>

      {page === "learning-calendar" ? <section className="settings-page page-container" aria-label="学习日历">
        <header className="settings-header">
          <button type="button" className="icon-button" onClick={() => navigate("home")} aria-label="返回首页"><Icon name="back" /></button>
          <h1>学习日历</h1><span className="header-spacer" />
        </header>
        <LearningCalendar timezone={user.timezone} onSignedOut={onSignedOut} />
      </section> : null}

      {page === "settings" ? (
        <SettingsPage user={user} settings={settings} wordLists={wordLists} speech={speech} voices={voices}
          settingsSaving={settingsSaving}
          speechSupported={speechSupported} onBack={() => navigate("home")}
          onSetting={updateSetting} onOpenWordList={(listId) => navigate(`word-list/${encodeURIComponent(listId)}`)} onSpeech={updateSpeech}
          onSignOut={signOut} onSessionExpired={onSignedOut} onOpenMutedWords={() => navigate("muted-words")} />
      ) : null}

      {page === "muted-words" ? <section className="settings-page page-container muted-words-page" aria-label="不再学习的单词管理">
        <header className="settings-header">
          <button type="button" className="icon-button" onClick={() => navigate("settings")} aria-label="返回设置"><Icon name="back" /></button>
          <h1>不再学习的单词</h1><span className="header-spacer" />
        </header>
        <MutedWords onRestore={restoreMutedWord} onSignedOut={onSignedOut} readCache={readCache} />
      </section> : null}

      {isWordListPage ? (
        <WordListPage list={currentWordList} ready={wordListsReady} settingsSaving={settingsSaving}
          selected={currentWordList ? settings.selected_word_list_ids.includes(currentWordList.list_id) : false}
          onBack={() => navigate("settings")} onToggle={toggleWordList} />
      ) : null}

      {message && page !== "study" ? <p role="alert" className="global-error error-notice">{message}</p> : null}
      {page !== "study" ? (
        <nav className="bottom-nav" aria-label="主导航">
          <button type="button" onClick={() => navigate("home")} aria-current={page === "home" || page === "learning-calendar" ? "page" : undefined}>
            <Icon name="home" /><span>首页</span>
          </button>
          <button type="button" className="nav-study" onClick={() => { void enableLearningReminder(); navigate("study"); }}>
            <span className="nav-study-icon"><Icon name="arrow" /></span>
            <span>{hasStarted ? "继续学习" : "开始学习"}</span>
          </button>
          <button type="button" onClick={() => navigate("settings")} aria-current={page === "settings" || page === "muted-words" || isWordListPage ? "page" : undefined}>
            <Icon name="settings" /><span>设置</span>
          </button>
        </nav>
      ) : null}
    </main>
  );
}

function Home({ user, stats, ready, refreshing, onRefresh, onOpenCalendar }: {
  user: User; stats: Stats; ready: boolean; refreshing: boolean;
  onRefresh: () => void;
  onOpenCalendar: () => void;
}) {
  const value = (count: number) => ready ? count.toLocaleString() : "—";
  return (
    <section className="home-page page-container" aria-label="首页">
      <header className="page-header">
        <div><span className="eyebrow">CONTEXT · 语境学词</span><h1>你好，{user.username}</h1></div>
      </header>
      <div className="home-intro"><span className="streak-badge"><Icon name="spark" /> 连续学习 {value(stats.streak_days)} 天</span></div>
      <section className="today-panel panel">
        <div className="section-heading"><h2>今日学习</h2></div>
        <div className="today-metrics">
          <div><strong>{value(stats.today_completed_cards)}</strong><span>今日完成卡片</span></div>
          <div><strong>{value(stats.due_words)}</strong><span>即刻复习</span></div>
        </div>
      </section>
      <button type="button" className="calendar-entry panel" onClick={onOpenCalendar}>
        <Icon name="calendar" /><strong>学习日历</strong><Icon name="chevron" />
      </button>
      <section className="overview-section">
        <div className="section-heading"><h2>学习概览</h2><button type="button" className="text-button" onClick={onRefresh} disabled={refreshing} aria-busy={refreshing} aria-label="刷新学习数据"><Icon name="refresh" /> {refreshing ? "刷新中…" : "刷新"}</button></div>
        <div className="overview-grid">
          <Metric icon="book" label="累计学过" value={value(stats.total_learned)} />
          <Metric icon="refresh" label="学习中的单词" value={value(stats.learning)} />
          <Metric icon="spark" label="可学新词" value={value(stats.new_words)} />
          <Metric icon="check" label="长期熟记" value={value(stats.mature)} />
        </div>
        <div className="study-time panel">
          <span><Icon name="clock" />总学习时长</span>
          <strong>{ready ? formatStudyTime(stats.total_study_time_ms) : "—"}</strong>
        </div>
      </section>
      <details className="learning-details panel">
        <summary><span><Icon name="chart" /> 更多学习数据</span><Icon name="chevron" /></summary>
        <dl className="detail-metrics">
          <div><dt>独立作答正确率</dt><dd>{ready && stats.today_independent_accuracy !== null ? `${stats.today_independent_accuracy}%` : "—"}</dd></div>
          <div><dt>有过错误的单词</dt><dd>{value(stats.lapse_words)}</dd></div>
          <div><dt>已进入间隔复习</dt><dd>{value(stats.mastered)}</dd></div>
          <div><dt>已学义项</dt><dd>{value(stats.learned_senses)}</dd></div>
          <div><dt>尚未学习的义项</dt><dd>{value(stats.new_senses)}</dd></div>
          <div><dt>进入间隔复习的义项</dt><dd>{value(stats.mastered_senses)}</dd></div>
        </dl>
        {ready && stats.legacy_unmapped_words > 0 ? <p className="sense-hint">{stats.legacy_unmapped_words} 个旧词的义项待确认</p> : null}
      </details>
    </section>
  );
}

function Metric({ icon, label, value }: { icon: IconName; label: string; value: string }) {
  return <div className="metric-card panel"><span className="metric-icon"><Icon name={icon} /></span><strong>{value}</strong><span className="metric-label">{label}</span></div>;
}

function SettingsPage({ user, settings, wordLists, settingsSaving, speech, voices, speechSupported, onBack, onSetting, onOpenWordList, onSpeech, onSignOut, onSessionExpired, onOpenMutedWords }: {
  user: User; settings: Settings; wordLists: WordList[]; speech: SpeechSettings; voices: SpeechSynthesisVoice[];
  settingsSaving: boolean;
  speechSupported: boolean; onBack: () => void; onSetting: (next: Partial<Settings>) => void;
  onOpenWordList: (listId: string) => void;
  onSpeech: (next: Partial<SpeechSettings>) => void; onSignOut: () => void; onSessionExpired: () => void;
  onOpenMutedWords: () => void;
}) {
  return (
    <section className="settings-page page-container" aria-label="设置">
      <header className="settings-header"><button type="button" className="icon-button" onClick={onBack} aria-label="返回首页"><Icon name="back" /></button><h1>设置</h1><span className="header-spacer" /></header>
      <div className="account-card panel"><span className="account-avatar"><Icon name="user" /></span><div><h2>{user.username}</h2><p>{user.timezone}</p></div></div>
      <section className="settings-group panel">
        <h2><Icon name="book" /> 智能复习</h2>
        <p className="settings-hint">根据你的作答表现自动安排复习。</p>
        <label className="setting-row"><span><strong>跳过基础 600 词</strong></span><input type="checkbox" className="setting-switch" disabled={settingsSaving} checked={settings.skip_basic_600} onChange={(event) => onSetting({ skip_basic_600: event.target.checked })} /></label>
        <button type="button" className="word-list-option" onClick={onOpenMutedWords}>
          <strong>不再学习的单词</strong><Icon name="chevron" />
        </button>
      </section>
      <section className="settings-group panel">
        <h2><Icon name="book" /> 学习显示</h2>
        <label className="setting-row"><span><strong>句子中文翻译</strong></span><input type="checkbox" className="setting-switch" disabled={settingsSaving} checked={settings.show_sentence_translation} onChange={(event) => onSetting({ show_sentence_translation: event.target.checked })} /></label>
      </section>
      <section className="settings-group panel">
        <h2><Icon name="book" /> 学习词库</h2>
        {wordLists.length === 0 ? <p className="settings-hint">暂无词库</p> : (
          <div className="word-list-picker">
            {wordLists.map((list) => (
              <button type="button" className="word-list-option" key={list.list_id}
                onClick={() => onOpenWordList(list.list_id)}>
                <strong>{list.title}</strong><Icon name="chevron" />
              </button>
            ))}
          </div>
        )}
      </section>
      <section className="settings-group panel">
        <h2><Icon name="sound" /> 发音</h2>
        {!speechSupported ? <p className="warning-notice">当前浏览器不支持语音朗读。</p> : (
          <div className="speech-controls">
            <label className="setting-field"><span>英文语音</span><select value={speech.voiceURI} onChange={(event) => onSpeech({ voiceURI: event.target.value })} disabled={voices.length === 0}>
              {voices.length === 0 ? <option value="">暂无可用英文语音</option> : voices.map((voice) => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang}</option>)}
            </select></label>
            <div className="voice-shortcuts"><button type="button" className="secondary-button" disabled={voices.length === 0} onClick={() => onSpeech({ voiceURI: preferredVoice(voices, "en-US") })}>美式语音</button><button type="button" className="secondary-button" disabled={voices.length === 0} onClick={() => onSpeech({ voiceURI: preferredVoice(voices, "en-GB") })}>英式语音</button></div>
            <div className="setting-field"><span>语速</span><div className="speech-speed" role="group" aria-label="语速">
              {SPEECH_SPEEDS.map(({ label, rate }) => <button key={rate} type="button" className="secondary-button" aria-pressed={speech.rate === rate} onClick={() => onSpeech({ rate })}>{label}</button>)}
            </div></div>
          </div>
        )}
      </section>
      <details className="learning-details panel admin-security">
        <summary><span><Icon name="user" /> 账号安全</span><Icon name="chevron" /></summary>
        <div className="pb-4"><PasskeySettings onSignedOut={onSessionExpired} /><PasswordForm /></div>
      </details>
      <button type="button" className="sign-out-button secondary-button" onClick={onSignOut}>退出登录</button>
      <p className="app-version">版本 {version}</p>
    </section>
  );
}

function WordListPage({ list, ready, selected, settingsSaving, onBack, onToggle }: {
  list: WordList | undefined; ready: boolean; selected: boolean; settingsSaving: boolean;
  onBack: () => void; onToggle: (listId: string, selected: boolean) => void;
}) {
  return (
    <section className="settings-page page-container" aria-label="词库详情">
      <header className="settings-header">
        <button type="button" className="icon-button" onClick={onBack} aria-label="返回设置"><Icon name="back" /></button>
        <h1>{list?.title ?? "词库"}</h1><span className="header-spacer" />
      </header>
      {list ? <>
        <section className="word-list-progress panel" aria-label="词库学习进度">
          <dl className="word-list-statistics">
            <div><dt>总单词</dt><dd>{list.word_count.toLocaleString()}</dd></div>
            <div><dt>已学习</dt><dd>{list.learned_word_count.toLocaleString()}</dd></div>
            <div><dt>已掌握</dt><dd>{list.mastered_word_count.toLocaleString()}</dd></div>
          </dl>
          <progress aria-label="已学习单词进度" value={list.learned_word_count} max={Math.max(1,list.word_count)} />
        </section>
        <section className="settings-group panel">
          <label className="setting-row word-list-selection">
            <strong>加入学习</strong>
            <input type="checkbox" className="setting-switch" disabled={settingsSaving} checked={selected}
              onChange={(event) => onToggle(list.list_id,event.target.checked)} />
          </label>
        </section>
        <details className="learning-details word-list-description panel">
          <summary><span>词库说明</span><Icon name="chevron" /></summary>
          <div>
            {list.description ? <p>{list.description}</p> : null}
            <p>取消词库不影响已有复习</p>
            {list.source_url ? <a href={list.source_url} target="_blank" rel="noreferrer">词表来源</a> : null}
          </div>
        </details>
      </> : <p className="settings-hint" role="status">{ready ? "词库不存在" : "加载中…"}</p>}
    </section>
  );
}

type IconName = "home" | "settings" | "arrow" | "back" | "more" | "sound" | "book" | "spark" | "check" | "refresh" | "chart" | "chevron" | "user" | "clock" | "calendar";

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, React.ReactNode> = {
    home: <><path d="m3 10 9-7 9 7" /><path d="M5 9v12h5v-7h4v7h5V9" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    calendar: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M7 3v4m10-4v4M3 11h18m-14 4h2m3 0h2m-7 3h2" /></>,
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
