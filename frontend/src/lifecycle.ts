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
  const checking = useRef<Promise<void> | undefined>(undefined);
  const lastChecked = useRef(0);
  const active = useRef(false);
  function check(force = false): Promise<void> {
    if (checking.current) return checking.current;
    if (!force && (document.hidden || !navigator.onLine || Date.now() - lastChecked.current < 30000)) return Promise.resolve();
    checking.current = (async () => {
      try {
        const payload = await request<{ version: string | null }>("/api/version");
        if (!payload.version) throw new Error("暂时无法获取版本信息");
        if (active.current) {
          setAvailable(payload.version !== version);
          lastChecked.current = Date.now();
        }
      } finally {
        checking.current = undefined;
      }
    })();
    return checking.current;
  }
  // Background failures stay silent; a manual refresh receives the error.
  const backgroundCheck = () => { void check().catch(() => {}); };
  useForegroundRefresh(backgroundCheck);
  useEffect(() => {
    active.current = true;
    backgroundCheck();
    const timer = window.setInterval(backgroundCheck, 5 * 60000);
    return () => { active.current = false; window.clearInterval(timer); };
  }, []);
  return { available, check: () => check(true) };
}
