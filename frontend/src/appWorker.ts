let registration: Promise<ServiceWorkerRegistration> | undefined;

// Register independently of notification permission: every app needs its shell.
export function registerAppWorker(): Promise<ServiceWorkerRegistration> {
  if (!window.isSecureContext || !("serviceWorker" in navigator)) return Promise.reject(new Error("Service Worker unavailable"));
  registration ??= navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })
    .catch((error) => { registration = undefined; throw error; });
  return registration;
}

export async function activateAppUpdate() {
  if (!window.isSecureContext || !("serviceWorker" in navigator)) return;
  let worker = await registerAppWorker();
  // An install-complete notification can arrive before the browser moves the
  // worker to waiting. Finish that installation rather than starting another.
  if (!worker.waiting && !worker.installing) await worker.update();
  const installing = worker.installing;
  if (installing) await new Promise<void>((resolve, reject) => {
    const done = () => {
      installing.removeEventListener("statechange", changed);
      window.clearTimeout(timer);
    };
    const changed = () => {
      if (installing.state === "installed") { done(); resolve(); }
      else if (installing.state === "redundant") { done(); reject(new Error("App update incomplete")); }
    };
    const timer = window.setTimeout(() => { done(); reject(new Error("App update timeout")); }, 15000);
    installing.addEventListener("statechange", changed);
    changed();
  });
  worker = await navigator.serviceWorker.getRegistration() ?? worker;
  if (worker.waiting) await new Promise<void>((resolve, reject) => {
    const done = () => {
      navigator.serviceWorker.removeEventListener("controllerchange", changed);
      window.clearTimeout(timer);
    };
    const changed = () => { done(); resolve(); };
    const timer = window.setTimeout(() => { done(); reject(new Error("App activation timeout")); }, 5000);
    navigator.serviceWorker.addEventListener("controllerchange", changed);
    worker.waiting!.postMessage({ type: "ACTIVATE_UPDATE" });
  });
}
