//! Legacy COSE credentials are verified by webauthn-rs-core with explicit UV/origin policy.
use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Duration;
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use webauthn_rs_core::{WebauthnCore, proto::*};

fn invalid() -> Fault {
    fault(400, "通行密钥验证失败，请重试或使用密码登录。")
}
fn decode(value: &str) -> Result<Vec<u8>> {
    URL_SAFE_NO_PAD
        .decode(value.trim_end_matches('='))
        .map_err(|_| invalid())
}
fn blob(value: &Value) -> Result<Vec<u8>> {
    value
        .as_array()
        .ok_or_else(invalid)?
        .iter()
        .map(|v| {
            v.as_u64()
                .filter(|n| *n <= 255)
                .map(|n| n as u8)
                .ok_or_else(invalid)
        })
        .collect()
}
fn core(config: &Config) -> Result<WebauthnCore> {
    if config.rp_id.parse::<std::net::IpAddr>().is_ok()
        || config.rp_id.is_empty()
        || config.rp_name.is_empty()
        || config.origins.is_empty()
    {
        return Err(fault(500, "Invalid WebAuthn configuration"));
    }
    let origins = config
        .origins
        .iter()
        .map(|origin| {
            let url = url::Url::parse(origin).map_err(|_| fault(500, "Invalid WebAuthn origin"))?;
            let host = url.host_str().unwrap_or("");
            if (url.scheme() != "https" && !(url.scheme() == "http" && host == "localhost"))
                || !url.username().is_empty()
                || url.password().is_some()
                || url.query().is_some()
                || url.fragment().is_some()
                || !((host == config.rp_id) || host.ends_with(&format!(".{}", config.rp_id)))
                || origin.split("://").nth(1).is_some_and(|v| v.contains('/'))
            {
                return Err(fault(500, "Invalid WebAuthn origin"));
            }
            Ok(url)
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(WebauthnCore::new_unsafe_experts_only(
        &config.rp_name,
        &config.rp_id,
        origins,
        std::time::Duration::from_secs(120),
        Some(false),
        Some(false),
    ))
}
fn trusted<'a>(r: &'a Request, config: &Config) -> Result<&'a str> {
    r.origin
        .as_deref()
        .filter(|origin| config.origins.iter().any(|v| v == origin))
        .ok_or_else(|| {
            fault(
                403,
                "当前站点未启用通行密钥，请使用密码登录或联系管理员配置 HTTPS 域名。",
            )
        })
}
fn confirm(p: &Value, user: &Value) -> Result<()> {
    let password = string(p, "current_password", 1, 128)?;
    if !accounts::verify_password(password, s(user, "password_hash")) {
        return Err(fault(403, "当前密码不正确。"));
    }
    Ok(())
}
fn cookie(config: &Config, purpose: &str, token: &str, expires: bool) -> String {
    format!(
        "cvt_passkey_{purpose}={token}; HttpOnly; Max-Age={}; Path=/api/auth/passkeys; SameSite=lax{}",
        if expires { 0 } else { 300 },
        if config.cookie_secure { "; Secure" } else { "" }
    )
}
fn store<T: Serialize>(
    c: &Connection,
    config: &Config,
    r: &Request,
    purpose: &str,
    state: &T,
    user: Option<i64>,
    name: Option<&str>,
) -> Result<String> {
    let token = accounts::random_token(32);
    let origin = trusted(r, config)?;
    exec(
        c,
        "DELETE FROM webauthn_challenges WHERE expires_at<=?",
        &[json!(config.stamp())],
    )?;
    if let Some(previous) = r.cookies.get(&format!("cvt_passkey_{purpose}")) {
        exec(
            c,
            "DELETE FROM webauthn_challenges WHERE token_hash=?",
            &[json!(accounts::token_hash(previous))],
        )?;
    }
    let bytes = serde_json::to_vec(state).map_err(|_| invalid())?;
    let session_hash = user.map(|_| accounts::token_hash(r.token.as_deref().unwrap_or("")));
    c.execute("INSERT INTO webauthn_challenges(token_hash,purpose,challenge,origin,user_id,session_hash,name,expires_at) VALUES(?,?,?,?,?,?,?,?)",
        params![accounts::token_hash(&token),purpose,bytes,origin,user,session_hash,name,(config.now()+Duration::seconds(300)).to_rfc3339_opts(SecondsFormat::Secs,false)])?;
    Ok(cookie(config, purpose, &token, false))
}
fn consume(
    c: &Connection,
    config: &Config,
    r: &Request,
    purpose: &str,
    user: Option<i64>,
) -> Result<Value> {
    let origin = trusted(r, config)?;
    let token = r
        .cookies
        .get(&format!("cvt_passkey_{purpose}"))
        .map(String::as_str)
        .unwrap_or("");
    c.execute_batch("BEGIN IMMEDIATE")?;
    let row = one(
        c,
        "SELECT * FROM webauthn_challenges WHERE token_hash=?",
        &[json!(accounts::token_hash(token))],
    )?;
    exec(
        c,
        "DELETE FROM webauthn_challenges WHERE token_hash=?",
        &[json!(accounts::token_hash(token))],
    )?;
    c.execute_batch("COMMIT")?; // Consume even if origin/state/cryptographic validation subsequently fails.
    let row = row.ok_or_else(|| fault(400, "通行密钥请求已过期或失效，请重试。"))?;
    if s(&row, "purpose") != purpose
        || s(&row, "expires_at") <= config.stamp().as_str()
        || s(&row, "origin") != origin
        || row["user_id"].as_i64() != user
        || (user.is_some()
            && s(&row, "session_hash") != accounts::token_hash(r.token.as_deref().unwrap_or("")))
    {
        return Err(fault(400, "通行密钥请求已过期或失效，请重试。"));
    }
    Ok(row)
}
fn client_data(credential: &Value, expected_origin: &str) -> Result<()> {
    if credential.to_string().len() > 65536 {
        return Err(invalid());
    }
    let data: Value =
        serde_json::from_slice(&decode(s(&credential["response"], "clientDataJSON"))?)
            .map_err(|_| invalid())?;
    if data["origin"].as_str() != Some(expected_origin) {
        return Err(invalid());
    }
    if (!data["crossOrigin"].is_null() && data["crossOrigin"] != json!(false))
        || !data["topOrigin"].is_null()
    {
        return Err(invalid());
    }
    Ok(())
}
fn state<T: for<'de> Deserialize<'de>>(row: &Value) -> Result<T> {
    serde_json::from_slice(&blob(&row["challenge"])?)
        .map_err(|_| fault(400, "通行密钥请求已过期或失效，请重试。"))
}
pub fn handle(c: &Connection, config: &Config, r: &Request) -> Result<Reply> {
    let path = r.path.trim_start_matches("/api/auth/passkeys");
    let p = &r.body;
    if path == "/config" && r.method == "GET" {
        core(config)?;
        return Ok(json!({"rp_id":config.rp_id,"origins":config.origins}).into());
    }
    let engine = core(config)?;
    if path == "/login/options" && r.method == "POST" {
        trusted(r, config)?;
        let builder = engine
            .new_challenge_authenticate_builder(Vec::new(), Some(UserVerificationPolicy::Required))
            .map_err(|_| invalid())?;
        let (challenge, state) = engine
            .generate_challenge_authenticate(builder)
            .map_err(|_| invalid())?;
        c.execute_batch("BEGIN IMMEDIATE")?;
        let cookie = store(c, config, r, "authentication", &state, None, None)?;
        c.execute_batch("COMMIT")?;
        let value = serde_json::to_value(challenge).map_err(|_| invalid())?;
        return Ok(Reply {
            body: json!({"options":value["publicKey"]}),
            cookies: vec![cookie],
        });
    }
    if path == "/login/verify" && r.method == "POST" {
        let challenge = consume(c, config, r, "authentication", None)?;
        client_data(&p["credential"], s(&challenge, "origin"))?;
        let credential_id = decode(s(&p["credential"], "id"))?;
        let user_handle = decode(s(&p["credential"]["response"], "userHandle"))?;
        c.execute_batch("BEGIN IMMEDIATE")?;
        let id: Option<i64> = c
            .query_row(
                "SELECT id FROM passkeys WHERE credential_id=?",
                [&credential_id],
                |r| r.get(0),
            )
            .optional()?;
        let key=one(c,"SELECT p.*,w.user_handle FROM passkeys p JOIN webauthn_users w ON w.user_id=p.user_id WHERE p.id=?",&[json!(id.ok_or_else(invalid)?)])?.ok_or_else(invalid)?;
        if !bool::from(user_handle.ct_eq(&blob(&key["user_handle"])?)) {
            return Err(invalid());
        }
        let value: serde_cbor_2::Value =
            serde_cbor_2::from_slice(&blob(&key["public_key"])?).map_err(|_| invalid())?;
        let cose = COSEKey::try_from(&value).map_err(|_| invalid())?;
        let mut cred: Credential = CredentialV3 {
            cred_id: credential_id,
            cred: cose,
            counter: n(&key, "sign_count") as u32,
            verified: true,
            registration_policy: UserVerificationPolicy::Required,
        }
        .into();
        cred.backup_eligible = s(&key, "device_type") == "multi_device";
        cred.backup_state = b(&key, "backed_up");
        cred.transports = serde_json::from_str(s(&key, "transports")).ok();
        let mut auth_state: AuthenticationState = state(&challenge)?;
        auth_state.set_allowed_credentials(vec![cred]);
        let response: PublicKeyCredential =
            serde_json::from_value(p["credential"].clone()).map_err(|_| invalid())?;
        let verified = engine
            .authenticate_credential(&response, &auth_state)
            .map_err(|_| invalid())?;
        if !verified.user_verified()
            || verified.backup_eligible() != (s(&key, "device_type") == "multi_device")
        {
            return Err(invalid());
        }
        exec(
            c,
            "UPDATE passkeys SET sign_count=?,backed_up=?,last_used_at=? WHERE id=?",
            &[
                json!(verified.counter()),
                json!(verified.backup_state()),
                json!(config.stamp()),
                key["id"].clone(),
            ],
        )?;
        let user = one(
            c,
            "SELECT * FROM users WHERE id=?",
            &[key["user_id"].clone()],
        )?
        .ok_or_else(invalid)?;
        let session = accounts::session(c, config, n(&user, "id"))?;
        c.execute_batch("COMMIT")?;
        return Ok(Reply {
            body: json!({"user":accounts::public_user(&user)}),
            cookies: vec![session, cookie(config, "authentication", "", true)],
        });
    }
    let user = accounts::authenticate(c, config, r)?;
    let uid = n(&user, "id");
    if path.is_empty() && r.method == "GET" {
        return Ok(json!({"passkeys":rows(c,"SELECT id,name,created_at,last_used_at FROM passkeys WHERE user_id=? ORDER BY id DESC",&[json!(uid)])?}).into());
    }
    if path == "/register/options" && r.method == "POST" {
        trusted(r, config)?;
        confirm(p, &user)?;
        let name = if p.get("name").is_none() {
            "我的通行密钥"
        } else {
            string(p, "name", 1, 64)?
        }
        .trim();
        if name.is_empty() {
            return Err(fault(400, "请输入通行密钥名称。"));
        }
        c.execute_batch("BEGIN IMMEDIATE")?;
        if lexicon::scalar(
            c,
            "SELECT COUNT(*) FROM passkeys WHERE user_id=?",
            &[json!(uid)],
        )?
        .as_i64()
        .unwrap_or(0)
            >= 20
        {
            return Err(fault(400, "通行密钥数量已达上限，请先删除不再使用的密钥。"));
        }
        let handle = decode(&accounts::random_token(32))?;
        c.execute(
            "INSERT OR IGNORE INTO webauthn_users(user_id,user_handle) VALUES(?,?)",
            params![uid, handle],
        )?;
        let handle: Vec<u8> = c.query_row(
            "SELECT user_handle FROM webauthn_users WHERE user_id=?",
            [uid],
            |r| r.get(0),
        )?;
        let mut statement = c.prepare("SELECT credential_id FROM passkeys WHERE user_id=?")?;
        let existing = statement
            .query_map([uid], |r| r.get::<_, Vec<u8>>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .into_iter()
            .map(Into::into)
            .collect();
        let builder = engine
            .new_challenge_register_builder(&handle, s(&user, "username"), s(&user, "username"))
            .map_err(|_| invalid())?
            .user_verification_policy(UserVerificationPolicy::Required)
            .require_resident_key(true)
            .exclude_credentials(Some(existing));
        let (challenge, state) = engine
            .generate_challenge_register(builder)
            .map_err(|_| invalid())?;
        let cookie = store(c, config, r, "registration", &state, Some(uid), Some(name))?;
        c.execute_batch("COMMIT")?;
        let value = serde_json::to_value(challenge).map_err(|_| invalid())?;
        return Ok(Reply {
            body: json!({"options":value["publicKey"]}),
            cookies: vec![cookie],
        });
    }
    if path == "/register/verify" && r.method == "POST" {
        let challenge = consume(c, config, r, "registration", Some(uid))?;
        client_data(&p["credential"], s(&challenge, "origin"))?;
        let state: RegistrationState = state(&challenge)?;
        let response: RegisterPublicKeyCredential =
            serde_json::from_value(p["credential"].clone()).map_err(|_| invalid())?;
        let verified = engine
            .register_credential(&response, &state, None)
            .map_err(|_| invalid())?;
        if !verified.user_verified {
            return Err(invalid());
        }
        let raw = decode(s(&p["credential"]["response"], "attestationObject"))?;
        let value: serde_cbor_2::Value = serde_cbor_2::from_slice(&raw).map_err(|_| invalid())?;
        let serde_cbor_2::Value::Map(map) = value else {
            return Err(invalid());
        };
        let Some(serde_cbor_2::Value::Bytes(data)) =
            map.get(&serde_cbor_2::Value::Text("authData".into()))
        else {
            return Err(invalid());
        };
        if data.len() < 55 {
            return Err(invalid());
        }
        let length = u16::from_be_bytes([data[53], data[54]]) as usize;
        let key_bytes = data.get(55 + length..).ok_or_else(invalid)?;
        let mut decoder = serde_cbor_2::Deserializer::from_slice(key_bytes);
        let cose = serde_cbor_2::Value::deserialize(&mut decoder).map_err(|_| invalid())?;
        let public_key = serde_cbor_2::to_vec(&cose).map_err(|_| invalid())?;
        c.execute_batch("BEGIN IMMEDIATE")?;
        if lexicon::scalar(
            c,
            "SELECT COUNT(*) FROM passkeys WHERE user_id=?",
            &[json!(uid)],
        )?
        .as_i64()
        .unwrap_or(0)
            >= 20
        {
            return Err(fault(400, "通行密钥数量已达上限。"));
        }
        let inserted=c.execute("INSERT INTO passkeys(user_id,credential_id,public_key,sign_count,device_type,backed_up,transports,name,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            params![uid,verified.cred_id.as_ref(),public_key,verified.counter,if verified.backup_eligible{"multi_device"}else{"single_device"},verified.backup_state,
                p["credential"]["response"]["transports"].as_array().cloned().unwrap_or_default().into_iter().filter_map(|v|v.as_str().map(str::to_owned)).collect::<Vec<_>>().pipe_json(),s(&challenge,"name"),config.stamp()]);
        if let Err(error) = inserted {
            if matches!(error,rusqlite::Error::SqliteFailure(ref e,_)if e.code==rusqlite::ErrorCode::ConstraintViolation)
            {
                return Err(fault(409, "此通行密钥已经添加，请使用其他密钥。"));
            }
            return Err(error.into());
        }
        c.execute_batch("COMMIT")?;
        return Ok(Reply {
            body: json!({"status":"passkey added"}),
            cookies: vec![cookie(config, "registration", "", true)],
        });
    }
    if r.method == "DELETE" && path.starts_with('/') {
        trusted(r, config)?;
        confirm(p, &user)?;
        let id = path[1..]
            .parse::<i64>()
            .map_err(|_| fault(422, "Invalid passkey id"))?;
        c.execute_batch("BEGIN IMMEDIATE")?;
        if exec(
            c,
            "DELETE FROM passkeys WHERE id=? AND user_id=?",
            &[json!(id), json!(uid)],
        )? == 0
        {
            return Err(fault(404, "通行密钥不存在。"));
        }
        c.execute_batch("COMMIT")?;
        return Ok(json!({"status":"passkey deleted"}).into());
    }
    Err(fault(404, "Not Found"))
}

trait JsonString {
    fn pipe_json(self) -> String;
}
impl JsonString for Vec<String> {
    fn pipe_json(self) -> String {
        serde_json::to_string(&self).expect("string list")
    }
}
