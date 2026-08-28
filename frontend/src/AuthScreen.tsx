import { FormEvent, useState } from "react";
import { errorMessage, request } from "./api";
import type { User } from "./types";

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
  const [error, setError] = useState("");

  function switchMode(next: Mode) {
    setMode(next);
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
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

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#f7f7f4] px-4 py-10 text-gray-950">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold">Context Vocabulary Trainer</h1>
        <p className="mt-1 text-sm text-gray-600">通过语境回忆单词，而不是孤立背诵。</p>

        <div className="mt-7 flex border border-gray-300 bg-white">
          <TabButton active={mode === "login"} onClick={() => switchMode("login")}>
            登录
          </TabButton>
          <TabButton active={mode === "register"} onClick={() => switchMode("register")}>
            注册
          </TabButton>
        </div>

        <form onSubmit={submit} className="mt-5 space-y-4 border border-gray-300 bg-white px-5 py-5">
          <Field label="用户名">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoFocus
              className="h-11 w-full border border-gray-300 px-3 outline-none focus:border-gray-950"
            />
          </Field>

          <Field label="密码">
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              className="h-11 w-full border border-gray-300 px-3 outline-none focus:border-gray-950"
            />
          </Field>

          {mode === "register" ? (
            <>
              <Field label="时区" hint="决定“今天”和复习到期日如何计算">
                <input
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                  placeholder="Asia/Shanghai"
                  className="h-11 w-full border border-gray-300 px-3 outline-none focus:border-gray-950"
                />
              </Field>
              <Field label="邀请码" hint="服务器未设置邀请码时留空">
                <input
                  value={registrationCode}
                  onChange={(event) => setRegistrationCode(event.target.value)}
                  className="h-11 w-full border border-gray-300 px-3 outline-none focus:border-gray-950"
                />
              </Field>
            </>
          ) : null}

          {error ? (
            <p className="border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>
          ) : null}

          <button
            type="submit"
            disabled={submitting || !username || !password}
            className="h-11 w-full border border-gray-950 bg-gray-950 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-300"
          >
            {submitting ? "处理中" : mode === "login" ? "登录" : "创建账号"}
          </button>

          {mode === "register" ? (
            <p className="text-xs text-gray-500">密码至少 8 位。用户名 3–32 位，可用字母、数字、_ . -</p>
          ) : null}
        </form>
      </div>
    </main>
  );
}

function TabButton({
  active,
  onClick,
  children
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-11 flex-1 text-sm font-medium ${
        active ? "bg-gray-950 text-white" : "text-gray-700"
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
    <label className="block text-sm text-gray-700">
      <span className="font-medium">{label}</span>
      {hint ? <span className="ml-2 text-xs text-gray-500">{hint}</span> : null}
      <div className="mt-1">{children}</div>
    </label>
  );
}
