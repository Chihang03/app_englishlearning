export type ThemePreference = "system" | "light" | "dark";

const STORAGE_KEY = "context-theme";
let preference: ThemePreference = "system";
const listeners = new Set<() => void>();
let initialized = false;

function parsePreference(value: string | null): ThemePreference {
  return value === "light" || value === "dark" ? value : "system";
}

function applyTheme() {
  const dark = preference === "dark" ||
    (preference === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#181a20" : "#f7f7f2");
}

export function initializeTheme() {
  if (initialized) return;
  initialized = true;
  try { preference = parsePreference(localStorage.getItem(STORAGE_KEY)); } catch { /* Storage may be blocked. */ }
  applyTheme();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    preference = parsePreference(event.key === null ? null : event.newValue);
    applyTheme();
    listeners.forEach(listener => listener());
  });
}

export function getThemePreference(): ThemePreference { return preference; }

export function setThemePreference(next: ThemePreference) {
  preference = next;
  try { localStorage.setItem(STORAGE_KEY, next); } catch { /* Keep the in-memory choice. */ }
  applyTheme();
  listeners.forEach(listener => listener());
}

export function subscribeTheme(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
