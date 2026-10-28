import { useSyncExternalStore } from "react";
import { getThemePreference, setThemePreference, subscribeTheme } from "./theme";

export function ThemeToggle() {
  return (
    <button type="button" className="auth-theme-toggle" aria-label="切换浅色或深色"
      onClick={() => setThemePreference(document.documentElement.dataset.theme === "dark" ? "light" : "dark")}>
      <svg className="theme-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="3.5" />
        <path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42" />
      </svg>
      <svg className="theme-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20.5 13.1A8.5 8.5 0 0 1 10.9 3.5a8.5 8.5 0 1 0 9.6 9.6Z" />
      </svg>
    </button>
  );
}

export function ThemeSettings() {
  const preference = useSyncExternalStore(subscribeTheme, getThemePreference);
  return (
    <div className="theme-options" role="group" aria-label="外观">
      {([ ["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"] ] as const).map(([value, label]) => (
        <button key={value} type="button" className="secondary-button"
          aria-pressed={preference === value} onClick={() => setThemePreference(value)}>{label}</button>
      ))}
    </div>
  );
}
