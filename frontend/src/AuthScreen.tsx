import { FormEvent, useState } from "react";
import { errorMessage, request } from "./api";
import { loginWithPasskey, passkeyError, usePasskeyAvailability } from "./passkeys";
import type { User } from "./types";
import { ThemeSettings } from "./ThemeSettings";

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
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
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
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4 py-10 text-ink">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold">Context Vocabulary Trainer</h1>

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
              <p className="text-xs text-muted">{availability.ready
                ? "首次使用需在设置中添加通行密钥"
                : availability.reason}</p>
            </div>
          ) : null}
          <Field label="用户名">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              disabled={busy}
              autoFocus
              className="h-11 w-full border border-control-line px-3 outline-none focus:border-control"
            />
          </Field>

          <Field label="密码">
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
            disabled={busy || !username || !password}
            className="h-11 w-full border border-control bg-surface text-sm font-semibold text-ink disabled:cursor-not-allowed disabled:border-control-line disabled:text-muted"
          >
            {submitting ? "处理中" : mode === "login" ? "密码登录" : "创建账号"}
          </button>

          {mode === "register" ? (
            <p className="text-xs text-muted">密码至少 8 位；用户名 3–32 位，支持字母、数字、_ . -。</p>
          ) : null}
        </form>
        <ThemeSettings />
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
