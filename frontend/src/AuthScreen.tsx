import { FormEvent, useState } from "react";
import { errorMessage, request } from "./api";
import { loginWithPasskey, passkeyError, usePasskeyAvailability } from "./passkeys";
import type { User } from "./types";
import { ThemeToggle } from "./ThemeSettings";
import { requestOnboarding } from "./Onboarding";

type Mode = "login" | "register";

// Pre-fill the account timezone from the browser so due dates line up with the
// learner's actual days without them having to think about it.
function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

export function AuthScreen({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [timezone, setTimezone] = useState(browserTimezone);
  const [registrationCode, setRegistrationCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [passkeyBusy, setPasskeyBusy] = useState(false);
  const [error, setError] = useState("");
  const availability = usePasskeyAvailability();
  const busy = submitting || passkeyBusy;

  function switchMode(next: Mode) {
    setMode(next);
    setError("");
    setConfirmation("");
    setRecovering(false);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (mode === "register") {
      // The same rules are enforced by the server; checking here answers in Chinese at once.
      const problem = !/^[A-Za-z0-9_.-]{3,32}$/.test(username) ? "用户名需为 3–32 位字母、数字或 _ . -"
        : password.length < 8 ? "密码至少需要 8 位"
        : password !== confirmation ? "两次输入的密码不一致" : "";
      if (problem) { setError(problem); return; }
    }
    setSubmitting(true);
    setError("");
    try {
      const body =
        mode === "login"
          ? { username, password }
          : {
              username,
              password,
              timezone: timezone || undefined,
              registration_code: registrationCode || undefined
            };
      const payload = await request<{ user: User }>(`/api/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      if (mode === "register") {
        requestOnboarding(payload.user.id);
        // New learners start beyond function words and the most basic vocabulary.
        // The guide shows this switch, and Settings keeps it editable.
        await request("/api/settings", { method: "PATCH", body: JSON.stringify({ skip_basic_600: true }) }).catch(() => {});
      }
      onAuthenticated(payload.user);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  }

  async function signInWithPasskey() {
    if (busy || !availability.ready) return;
    setPasskeyBusy(true);
    setError("");
    try {
      onAuthenticated(await loginWithPasskey());
    } catch (caught) {
      setError(passkeyError(caught));
    } finally {
      setPasskeyBusy(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center bg-canvas px-4 py-10 text-ink">
      <ThemeToggle />
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold">语境学词 · Context</h1>

        <div className="mt-7 flex border border-control-line bg-surface">
          <TabButton disabled={busy} active={mode === "login"} onClick={() => switchMode("login")}>
            登录
          </TabButton>
          <TabButton disabled={busy} active={mode === "register"} onClick={() => switchMode("register")}>
            注册
          </TabButton>
        </div>

        <form onSubmit={submit} className="mt-5 space-y-4 border border-control-line bg-surface px-5 py-5">
          {mode === "login" ? (
            <div className="space-y-2 border-b border-line pb-4">
              <button type="button" onClick={() => void signInWithPasskey()}
                disabled={busy || !availability.ready}
                className="h-11 w-full border border-control bg-control text-sm font-semibold text-on-control disabled:cursor-not-allowed disabled:border-control-line disabled:bg-control-disabled">
                {passkeyBusy ? "正在验证通行密钥…" : "使用通行密钥登录"}
              </button>
              {availability.ready ? null : <p className="text-xs text-muted">{availability.reason}</p>}
            </div>
          ) : null}
          <Field label="用户名" hint={mode === "register" ? "3–32 位字母、数字或 _ . -" : undefined}>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              disabled={busy}
              autoFocus
              className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
            />
          </Field>

          <Field label="密码" hint={mode === "register" ? "至少 8 位" : undefined}>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              disabled={busy}
              className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
            />
          </Field>

          {mode === "register" ? (
            <>
              <Field label="确认密码">
                <input
                  type="password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  autoComplete="new-password"
                  disabled={busy}
                  className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
                />
              </Field>
              <Field label="时区">
                <input
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                  placeholder="Asia/Shanghai"
                  className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
                />
              </Field>
              <Field label="邀请码" hint="如无则留空">
                <input
                  value={registrationCode}
                  onChange={(event) => setRegistrationCode(event.target.value)}
                  className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
                />
              </Field>
            </>
          ) : null}

          {error ? (
            <p role="alert" className="border border-danger-line bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p>
          ) : null}

          <button
            type="submit"
            disabled={busy || !username || !password || (mode === "register" && !confirmation)}
            className="h-11 w-full border border-control bg-surface text-sm font-semibold text-ink disabled:cursor-not-allowed disabled:border-control-line disabled:text-muted"
          >
            {submitting ? "处理中" : mode === "login" ? "密码登录" : "创建账号"}
          </button>

          {mode === "login" ? (
            <p className="text-center text-xs text-muted">
              {recovering ? "请联系管理员重置密码，凭临时密码登录后在设置中修改。"
                : <button type="button" className="underline" onClick={() => setRecovering(true)}>忘记密码？</button>}
            </p>
          ) : null}
        </form>
      </div>
    </main>
  );
}

function TabButton({
  active,
  disabled,
  onClick,
  children
}: {
  active: boolean;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`h-11 flex-1 text-sm font-medium ${
        active ? "bg-control text-on-control" : "text-ink"
      }`}
    >
      {children}
    </button>
  );
}

function Field({
  label,
  hint,
  children
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-sm text-ink">
      <span className="font-medium">{label}</span>
      {hint ? <span className="ml-2 text-xs text-muted">{hint}</span> : null}
      <div className="mt-1">{children}</div>
    </label>
  );
}
