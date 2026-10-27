import { useSyncExternalStore } from "react";
import { getThemePreference, setThemePreference, subscribeTheme } from "./theme";

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
