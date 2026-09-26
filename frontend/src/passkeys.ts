import { useEffect, useState } from "react";
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON
} from "@simplewebauthn/browser";
import { errorMessage, request } from "./api";
import type { User } from "./types";

export function usePasskeyAvailability() {
  const [availability, setAvailability] = useState({ ready: false, canManage: false, reason: "正在检查通行密钥…" });

  useEffect(() => {
    let active = true;
    if (!window.isSecureContext) {
      setAvailability({ ready: false, canManage: false, reason: "通行密钥需要 HTTPS；本机开发可使用 localhost。" });
    } else {
      const supported = browserSupportsWebAuthn();
      request<{ origins: string[] }>("/api/auth/passkeys/config")
        .then(({ origins }) => {
          if (active) setAvailability(origins.includes(window.location.origin)
            ? { ready: supported, canManage: true, reason: supported ? "" : "当前浏览器不支持通行密钥，请使用密码登录。" }
            : { ready: false, canManage: false, reason: "当前站点尚未启用通行密钥，请联系管理员配置域名。" });
        })
        .catch(() => {
          if (active) setAvailability({ ready: false, canManage: false, reason: "暂时无法使用通行密钥，请使用密码登录。" });
        });
    }
    return () => { active = false; };
  }, []);

  return availability;
}

export function passkeyError(error: unknown): string {
  if (error instanceof Error) {
    if (error.name === "NotAllowedError") return "操作已取消、超时或没有可用的通行密钥。可重试或使用密码登录。";
    if (error.name === "InvalidStateError") return "此设备上的通行密钥已添加，请使用其他密钥。";
    if (error.name === "SecurityError") return "当前站点无法使用通行密钥，请检查 HTTPS 和域名配置。";
    if (error.name === "AbortError") return "通行密钥操作已取消，请重试。";
  }
  return errorMessage(error);
}

export async function loginWithPasskey(): Promise<User> {
  const { options } = await request<{ options: PublicKeyCredentialRequestOptionsJSON }>(
    "/api/auth/passkeys/login/options", { method: "POST" }
  );
  const credential = await startAuthentication({ optionsJSON: options });
  const { user } = await request<{ user: User }>("/api/auth/passkeys/login/verify", {
    method: "POST", body: JSON.stringify({ credential })
  });
  return user;
}

export async function addPasskey(name: string, currentPassword: string): Promise<void> {
  const { options } = await request<{ options: PublicKeyCredentialCreationOptionsJSON }>(
    "/api/auth/passkeys/register/options", {
      method: "POST", body: JSON.stringify({ name, current_password: currentPassword })
    }
  );
  const credential = await startRegistration({ optionsJSON: options });
  await request("/api/auth/passkeys/register/verify", {
    method: "POST", body: JSON.stringify({ credential })
  });
}
