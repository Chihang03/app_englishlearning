import { useEffect, useRef, useState } from "react";
import { request } from "./api";
import { version } from "./version.json";
import { watchForeground } from "./foreground";
export { canReloadForUpdate } from "./foreground";

export function useForegroundRefresh(refresh: () => void) {
  const latest = useRef(refresh);
  latest.current = refresh;
  useEffect(() => watchForeground(() => latest.current()), []);
}

export function useVersionUpdate() {
  const [available, setAvailable] = useState(false);
  const checking = useRef(false);
  const lastChecked = useRef(0);
  const active = useRef(false);
  async function check() {
    if (checking.current || document.hidden || !navigator.onLine || Date.now() - lastChecked.current < 30000) return;
    checking.current = true;
    try {
      const payload = await request<{ version: string | null }>("/api/version");
      if (active.current) {
        setAvailable(Boolean(payload.version && payload.version !== version));
        lastChecked.current = Date.now();
      }
    } catch {
      // A deployment check must not turn a temporary outage into a login error.
    } finally {
      checking.current = false;
    }
  }
  useForegroundRefresh(() => { void check(); });
  useEffect(() => {
    active.current = true;
    void check();
    const timer = window.setInterval(() => { void check(); }, 5 * 60000);
    return () => { active.current = false; window.clearInterval(timer); };
  }, []);
  return available;
}
