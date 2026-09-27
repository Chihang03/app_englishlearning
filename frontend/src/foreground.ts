export const RESUME_AFTER_MS = 30000;

export function watchForeground(refresh: () => void) {
  let hiddenAt = document.hidden ? Date.now() : null;
  const visibility = () => {
    if (document.hidden) hiddenAt = Date.now();
    else {
      const elapsed = hiddenAt === null ? 0 : Date.now() - hiddenAt;
      hiddenAt = null;
      if (elapsed >= RESUME_AFTER_MS) refresh();
    }
  };
  const restored = (event: PageTransitionEvent) => {
    if (event.persisted) refresh();
  };
  const online = () => { if (!document.hidden) refresh(); };
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pageshow", restored);
  window.addEventListener("online", online);
  return () => {
    document.removeEventListener("visibilitychange", visibility);
    window.removeEventListener("pageshow", restored);
    window.removeEventListener("online", online);
  };
}

export function canReloadForUpdate({ page, draft, busy }: { page: string; draft: boolean; busy: boolean }) {
  return page === "home" && !draft && !busy && !document.hidden && navigator.onLine;
}
