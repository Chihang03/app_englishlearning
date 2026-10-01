import { useEffect, useRef } from "react";
import { request } from "./api";

type PushConfig = { enabled: boolean; public_key: string | null };
let registration: Promise<ServiceWorkerRegistration> | undefined;

function supportsPush() {
  if (!window.isSecureContext || !("serviceWorker" in navigator) || !("PushManager" in window)
      || !("Notification" in window)) return false;
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  return !ios || window.matchMedia("(display-mode: standalone)").matches
    || (navigator as Navigator & { standalone?: boolean }).standalone === true;
}

function registerWorker() {
  registration ??= navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" })
    .then(() => navigator.serviceWorker.ready)
    .catch((error) => { registration = undefined; throw error; });
  return registration;
}

function applicationKey(value: string) {
  const decoded = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4));
  return Uint8Array.from(decoded, (char) => char.charCodeAt(0));
}

async function saveSubscription(subscription: PushSubscription) {
  await request("/api/push/subscription", { method: "POST", body: JSON.stringify(subscription.toJSON()) });
}

export function useLearningReminder(userId: number) {
  const config = useRef<PushConfig | null>(null);
  const enabling = useRef(false);
  const asked = useRef(false);

  useEffect(() => {
    if (!supportsPush()) return;
    let active = true;
    let syncing = false;
    const sync = async () => {
      if (syncing || document.hidden) return;
      syncing = true;
      try {
        const nextPermission = Notification.permission;
        const nextConfig = await request<PushConfig>("/api/push/config");
        if (!active) return;
        config.current = nextConfig;
        if (!nextConfig.enabled || !nextConfig.public_key) return;
        const worker = await registerWorker();
        if (!active) return;
        if (nextPermission === "granted") {
          const subscription = await worker.pushManager.getSubscription()
            ?? await worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationKey(nextConfig.public_key) });
          if (active) await saveSubscription(subscription);
        }
      } catch {
        // Foregrounding retries registration without interrupting learning.
      } finally {
        syncing = false;
      }
    };
    const foreground = () => { if (!document.hidden) void sync(); };
    void sync();
    document.addEventListener("visibilitychange", foreground);
    window.addEventListener("focus", foreground);
    return () => {
      active = false;
      document.removeEventListener("visibilitychange", foreground);
      window.removeEventListener("focus", foreground);
    };
  }, [userId]);

  async function enable() {
    if (!supportsPush() || enabling.current || Notification.permission === "denied"
        || (Notification.permission === "default" && asked.current) || config.current?.enabled === false) return;
    enabling.current = true;
    try {
      // Must run before any await so iOS sees the original button gesture.
      asked.current = true;
      const allowed = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      if (allowed !== "granted") return;
      const [worker, nextConfig] = await Promise.all([
        registerWorker(), config.current ? Promise.resolve(config.current) : request<PushConfig>("/api/push/config")
      ]);
      config.current = nextConfig;
      if (!nextConfig.enabled || !nextConfig.public_key) return;
      const subscription = await worker.pushManager.getSubscription()
        ?? await worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationKey(nextConfig.public_key) });
      await saveSubscription(subscription);
    } catch {
      // Retry on the next visit or study click; notification errors never block a card.
    } finally {
      enabling.current = false;
    }
  }

  return enable;
}
