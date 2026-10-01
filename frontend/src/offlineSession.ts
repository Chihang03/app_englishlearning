import type { User } from "./types";

const KEY = "cvt.last-learner.v1";

// This identifies a read-only local library; it never establishes a session.
export function rememberLearner(user: User | null) {
  try {
    if (user?.role === "learner") localStorage.setItem(KEY, JSON.stringify(user));
    else localStorage.removeItem(KEY);
  } catch { /* Offline browsing is optional when device storage is unavailable. */ }
}

export function savedLearner(): User | null {
  try {
    const user = JSON.parse(localStorage.getItem(KEY) ?? "null");
    return user && Number.isSafeInteger(user.id) && user.id > 0 && typeof user.username === "string" &&
      typeof user.timezone === "string" && user.role === "learner" ? user : null;
  } catch { return null; }
}
