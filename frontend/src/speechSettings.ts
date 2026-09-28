import { useEffect, useRef, useState } from "react";
import { isUnauthorized, request } from "./api";
import type { Settings, SpeechRate, SpeechSettings } from "./types";

// Keep the old 175 wpm baseline for the browser's rate multiplier.
export const BASE_WPM = 175;
export const DEFAULT_SPEECH_RATE: SpeechRate = 120;
export const SPEECH_SPEEDS = [
  { label: "慢", rate: 90 },
  { label: "正常", rate: 120 },
  { label: "快", rate: 175 }
] as const;

type RateCache = { rate: SpeechRate; pending: boolean };
const VOICE_KEY = "cvt.speech";
const rateKey = (userId: number) => `cvt.speech.rate.${userId}`;
const validRate = (rate: unknown): rate is SpeechRate => rate === 90 || rate === 120 || rate === 175;

function readVoice(): string {
  try {
    const stored = JSON.parse(localStorage.getItem(VOICE_KEY) ?? "null");
    return typeof stored?.voiceURI === "string" ? stored.voiceURI : "";
  } catch { return ""; }
}

function readRate(userId: number): RateCache {
  try {
    const stored = JSON.parse(localStorage.getItem(rateKey(userId)) ?? "null");
    if (validRate(stored?.rate)) return { rate: stored.rate, pending: stored.pending === true };
  } catch { /* Site data may be unavailable. */ }
  // The former slider's implicit 175 default must not become the new default.
  return { rate: DEFAULT_SPEECH_RATE, pending: false };
}

function writeCache(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch { /* Account saving still works when local storage is unavailable. */ }
}

export function useSpeechSettings(userId: number, onSignedOut: () => void,
  readSettings: () => Promise<Settings> = () => request<Settings>("/api/settings")) {
  const [speech, setSpeech] = useState<SpeechSettings>(() => ({ voiceURI: readVoice(), rate: readRate(userId).rate }));
  const current = useRef(readRate(userId));
  const revision = useRef(0);
  const signedOut = useRef(onSignedOut);
  signedOut.current = onSignedOut;
  const syncRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    let active = true;
    let running: Promise<void> | undefined;
    let loaded = false;
    let retry: number | undefined;
    const persist = () => writeCache(rateKey(userId), current.current);

    function sync(): Promise<void> {
      if (!active) return Promise.resolve();
      if (running) return running;
      window.clearTimeout(retry);
      running = Promise.resolve().then(async () => {
        try {
          if (!loaded && !current.current.pending) {
            const settings = await readSettings();
            if (!active) return;
            // A selection made during loading always takes precedence.
            if (!current.current.pending) {
              current.current = { rate: validRate(settings.speech_rate) ? settings.speech_rate : current.current.rate,
                pending: settings.speech_rate == null };
              persist();
              setSpeech((value) => ({ ...value, rate: current.current.rate }));
            }
          }
          loaded = true;
          // Serialize writes so rapid taps cannot save an older choice last.
          while (active && current.current.pending) {
            const sentRevision = revision.current;
            const rate = current.current.rate;
            await request<Settings>("/api/settings", { method: "PATCH", body: JSON.stringify({ speech_rate: rate }), keepalive: true });
            if (!active) return;
            if (revision.current === sentRevision) {
              current.current = { rate, pending: false };
              persist();
            }
          }
        } catch (error) {
          if (!active) return;
          if (isUnauthorized(error)) signedOut.current();
          else retry = window.setTimeout(backgroundSync, 5000);
          throw error;
        } finally {
          running = undefined;
        }
      });
      return running;
    }

    function backgroundSync() { void sync().catch(() => {}); }
    const resume = () => { if (!document.hidden) backgroundSync(); };
    syncRef.current = sync;
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", resume);
    backgroundSync();
    return () => {
      active = false;
      window.clearTimeout(retry);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [userId]);

  function updateSpeech(next: Partial<SpeechSettings>) {
    if (next.voiceURI !== undefined) writeCache(VOICE_KEY, { voiceURI: next.voiceURI });
    if (validRate(next.rate)) {
      revision.current += 1;
      current.current = { rate: next.rate, pending: true };
      writeCache(rateKey(userId), current.current);
    }
    setSpeech((value) => ({ ...value, ...next }));
    if (next.rate !== undefined) void syncRef.current().catch(() => {});
  }

  function applySpeechSettings(settings: Settings) {
    // A server download must never overwrite an unsaved local selection.
    if (current.current.pending || !validRate(settings.speech_rate)) return;
    current.current = { rate: settings.speech_rate, pending: false };
    writeCache(rateKey(userId), current.current);
    setSpeech((value) => ({ ...value, rate: current.current.rate }));
  }

  return { speech, updateSpeech, syncSpeech: () => syncRef.current(), applySpeechSettings };
}
