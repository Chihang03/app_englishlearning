import { FormEvent, useEffect, useState } from "react";
import { isUnauthorized, request } from "./api";
import { addPasskey, passkeyError, usePasskeyAvailability } from "./passkeys";

type Passkey = { id: number; name: string; created_at: string; last_used_at: string | null };

export function PasskeySettings({ onSignedOut }: { onSignedOut: () => void }) {
  const [open, setOpen] = useState(false);
  const [keys, setKeys] = useState<Passkey[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [name, setName] = useState("我的通行密钥");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const availability = usePasskeyAvailability();

  function reportError(caught: unknown) {
    if (isUnauthorized(caught)) onSignedOut();
    else setError(passkeyError(caught));
  }

  async function refresh() {
    const payload = await request<{ passkeys: Passkey[] }>("/api/auth/passkeys");
    setKeys(payload.passkeys);
  }

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setLoadFailed(false);
    request<{ passkeys: Passkey[] }>("/api/auth/passkeys")
      .then(({ passkeys }) => { if (active) setKeys(passkeys); })
      .catch((caught: unknown) => {
        if (active) { setLoadFailed(true); reportError(caught); }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy || !availability.ready) return;
    setBusy(true);
    setError("");
    setStatus("");
    setDeleteId(null);
    try {
      await addPasskey(name.trim(), password);
      setPassword("");
      setStatus("通行密钥已添加，下次可直接使用它登录。");
      await refresh();
    } catch (caught) { reportError(caught); }
    finally { setBusy(false); }
  }

  async function remove(id: number) {
    if (busy || !password) return;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await request(`/api/auth/passkeys/${id}`, {
        method: "DELETE", body: JSON.stringify({ current_password: password })
      });
      setPassword("");
      setDeleteId(null);
      setStatus("通行密钥已删除。设备或密码管理器中的副本可自行移除。");
      await refresh();
    } catch (caught) { reportError(caught); }
    finally { setBusy(false); }
  }

  if (!open) return (
    <button type="button" onClick={() => { setError(""); setStatus(""); setOpen(true); }}
      className="mt-3 h-10 w-full border border-gray-300 text-sm text-gray-700">
      管理通行密钥
    </button>
  );

  return (
    <section className="mt-3 space-y-3 border border-gray-300 p-3" aria-label="通行密钥管理">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">通行密钥</h3>
        <button type="button" disabled={busy} className="text-xs text-gray-600 disabled:text-gray-400"
          onClick={() => { setOpen(false); setPassword(""); setDeleteId(null); }}>收起</button>
      </div>
      <p className="text-xs text-gray-500">使用指纹、面容或设备 PIN 登录。建议添加备用密钥，密码登录仍可使用。</p>
      {loading ? <p className="text-xs text-gray-500">正在加载…</p> : loadFailed ? (
        <button type="button" className="text-xs underline" onClick={() => { setOpen(false); }}>加载失败，收起后可重试</button>
      ) : keys.length === 0 ? <p className="text-xs text-gray-500">尚未添加通行密钥。</p> : (
        <ul className="space-y-2">
          {keys.map((key) => (
            <li key={key.id} className="border border-gray-200 p-2">
              <p className="break-words text-sm font-medium">{key.name}</p>
              <p className="mt-1 text-xs text-gray-500">添加于 {new Date(key.created_at).toLocaleDateString()}</p>
              <p className="text-xs text-gray-500">{key.last_used_at
                ? `最近使用 ${new Date(key.last_used_at).toLocaleString()}` : "尚未用于登录"}</p>
              {deleteId === key.id ? (
                <div className="mt-2 space-y-2">
                  <p className="text-xs text-gray-600">确认删除？请在下方输入当前密码。</p>
                  <div className="flex gap-3">
                    <button type="button" disabled={busy || !password || !availability.canManage}
                      onClick={() => void remove(key.id)} className="text-xs text-red-700 disabled:text-gray-400">确认删除</button>
                    <button type="button" disabled={busy} onClick={() => setDeleteId(null)} className="text-xs text-gray-600">取消</button>
                  </div>
                </div>
              ) : (
                <button type="button" disabled={busy || !availability.canManage} onClick={() => setDeleteId(key.id)}
                  className="mt-2 text-xs text-red-700 disabled:text-gray-400">删除</button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!availability.ready ? <p className="text-xs text-amber-800">{availability.reason}</p> : null}
      <form onSubmit={create} className="space-y-2">
        <label className="block text-xs text-gray-600">当前密码（添加或删除时确认）
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password" disabled={busy} maxLength={128}
            className="mt-1 h-10 w-full border border-gray-300 px-2 text-sm outline-none focus:border-gray-950" />
        </label>
        <label className="block text-xs text-gray-600">新通行密钥名称
          <input value={name} onChange={(event) => setName(event.target.value)} maxLength={64} disabled={busy}
            className="mt-1 h-10 w-full border border-gray-300 px-2 text-sm outline-none focus:border-gray-950" />
        </label>
        <button type="submit" disabled={busy || loading || !availability.ready || !password || !name.trim()}
          className="h-10 w-full border border-gray-950 bg-gray-950 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-300">
          {busy ? "处理中…" : "添加通行密钥"}
        </button>
      </form>
      {error ? <p role="alert" className="text-xs text-red-700">{error}</p> : null}
      {status ? <p role="status" className="text-xs text-emerald-700">{status}</p> : null}
    </section>
  );
}
