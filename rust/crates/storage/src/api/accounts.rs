use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Duration;
use rand::{RngCore, rngs::OsRng};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

pub fn random_token(length: usize) -> String {
    let mut bytes = vec![0; length];
    OsRng.fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}
pub fn token_hash(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn unhex(value: &str) -> Option<Vec<u8>> {
    if value.len() % 2 != 0 {
        return None;
    }
    (0..value.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&value[i..i + 2], 16).ok())
        .collect()
}
pub fn hash_password(password: &str) -> Result<String> {
    let mut salt = [0u8; 16];
    OsRng.fill_bytes(&mut salt);
    let mut output = [0u8; 32];
    let parameters = scrypt::Params::new(14, 8, 1, 32).map_err(|e| fault(500, e.to_string()))?;
    scrypt::scrypt(password.as_bytes(), &salt, &parameters, &mut output)
        .map_err(|e| fault(500, e.to_string()))?;
    Ok(format!("scrypt$16384$8$1${}${}", hex(&salt), hex(&output)))
}
pub fn verify_password(password: &str, stored: &str) -> bool {
    let verify = || -> Option<bool> {
        let parts = stored.split('$').collect::<Vec<_>>();
        if parts.len() != 6 || parts[0] != "scrypt" {
            return None;
        }
        let count = parts[1].parse::<u64>().ok()?;
        if !count.is_power_of_two() || count > 262144 {
            return None;
        }
        let salt = unhex(parts[4])?;
        let expected = unhex(parts[5])?;
        if expected.is_empty() || expected.len() > 64 || salt.len() > 64 {
            return None;
        }
        let parameters = scrypt::Params::new(
            count.ilog2() as u8,
            parts[2].parse().ok()?,
            parts[3].parse().ok()?,
            expected.len(),
        )
        .ok()?;
        let mut output = vec![0; expected.len()];
        scrypt::scrypt(password.as_bytes(), &salt, &parameters, &mut output).ok()?;
        Some(bool::from(output.ct_eq(&expected)))
    };
    verify().unwrap_or(false)
}
pub fn public_user(user: &Value) -> Value {
    json!({"id":user["id"],"username":user["username"],"timezone":user["timezone"],"role":user["role"]})
}
pub fn authenticate(c: &Connection, config: &Config, r: &Request) -> Result<Value> {
    let token = r
        .token
        .as_deref()
        .ok_or_else(|| fault(401, "Not authenticated"))?;
    one(c,"SELECT u.* FROM sessions t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.expires_at>?",&[json!(token_hash(token)),json!(config.stamp())])?.ok_or_else(||fault(401,"Session expired"))
}
pub fn timezone(name: &str) -> Result<chrono_tz::Tz> {
    name.parse()
        .map_err(|_| fault(400, format!("Unknown timezone: {name}")))
}
pub fn session(c: &Connection, config: &Config, user_id: i64) -> Result<String> {
    exec(
        c,
        "DELETE FROM sessions WHERE expires_at<=?",
        &[json!(config.stamp())],
    )?;
    let token = random_token(32);
    let expires = (config.now() + Duration::days(30)).to_rfc3339_opts(SecondsFormat::Secs, false);
    exec(
        c,
        "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        &[
            json!(token_hash(&token)),
            json!(user_id),
            json!(config.stamp()),
            json!(expires),
        ],
    )?;
    Ok(format!(
        "cvt_session={token}; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax{}",
        if config.cookie_secure { "; Secure" } else { "" }
    ))
}
pub fn handle(c: &Connection, config: &Config, r: &Request) -> Result<Reply> {
    let path = r.path.as_str();
    let p = &r.body;
    if path == "/api/auth/register" && r.method == "POST" {
        let username = string(p, "username", 3, 32)?;
        if !username
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        {
            return Err(fault(422, "Invalid username"));
        }
        let password = string(p, "password", 8, 128)?;
        if username.eq_ignore_ascii_case("admin") {
            return Err(fault(409, "该用户名已预留"));
        }
        if !config.registration_code.is_empty()
            && s(p, "registration_code") != config.registration_code
        {
            return Err(fault(403, "Invalid registration code"));
        }
        let tz = p["timezone"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(&config.default_timezone);
        timezone(tz)?;
        if one(
            c,
            "SELECT id FROM users WHERE username=?",
            &[json!(username)],
        )?
        .is_some()
        {
            return Err(fault(409, "Username already taken"));
        }
        exec(
            c,
            "INSERT INTO users(username,password_hash,timezone,created_at) VALUES(?,?,?,?)",
            &[
                json!(username),
                json!(hash_password(password)?),
                json!(tz),
                json!(config.stamp()),
            ],
        )?;
        let id = c.last_insert_rowid();
        exec(
            c,
            "INSERT INTO user_vocabulary_lists(user_id,list_id,selected) SELECT ?,list_id,default_selected FROM vocabulary_lists",
            &[json!(id)],
        )?;
        let cookie = session(c, config, id)?;
        let user = one(c, "SELECT * FROM users WHERE id=?", &[json!(id)])?.unwrap();
        return Ok(Reply {
            body: json!({"user":public_user(&user)}),
            cookies: vec![cookie],
        });
    }
    if path == "/api/auth/login" && r.method == "POST" {
        let username = string(p, "username", 1, 32)?;
        let password = string(p, "password", 1, 128)?;
        let user = one(
            c,
            "SELECT * FROM users WHERE username=?",
            &[json!(username)],
        )?;
        let dummy = "scrypt$16384$8$1$00000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000";
        if !verify_password(
            password,
            user.as_ref().map_or(dummy, |u| s(u, "password_hash")),
        ) || user.is_none()
        {
            return Err(fault(401, "Incorrect username or password"));
        }
        let user = user.unwrap();
        let cookie = session(c, config, n(&user, "id"))?;
        return Ok(Reply {
            body: json!({"user":public_user(&user)}),
            cookies: vec![cookie],
        });
    }
    if path == "/api/auth/logout" && r.method == "POST" {
        if let Some(token) = &r.token {
            exec(
                c,
                "DELETE FROM push_subscriptions WHERE session_hash=?",
                &[json!(token_hash(token))],
            )?;
            exec(
                c,
                "DELETE FROM sessions WHERE token_hash=?",
                &[json!(token_hash(token))],
            )?;
        }
        return Ok(Reply {
            body: json!({"status":"signed out"}),
            cookies: vec!["cvt_session=\"\"; Max-Age=0; Path=/; SameSite=lax".into()],
        });
    }
    let user = authenticate(c, config, r)?;
    let id = n(&user, "id");
    if path == "/api/auth/me" && r.method == "GET" {
        return Ok(json!({"user":public_user(&user)}).into());
    }
    if path == "/api/auth/me" && r.method == "PATCH" {
        let tz = string(p, "timezone", 1, 64)?;
        timezone(tz)?;
        exec(
            c,
            "UPDATE users SET timezone=? WHERE id=?",
            &[json!(tz), json!(id)],
        )?;
        let user = one(c, "SELECT * FROM users WHERE id=?", &[json!(id)])?.unwrap();
        return Ok(json!({"user":public_user(&user)}).into());
    }
    if path == "/api/auth/password" && r.method == "PATCH" {
        let current = string(p, "current_password", 1, 128)?;
        let new = string(p, "new_password", 8, 128)?;
        if !verify_password(current, s(&user, "password_hash")) {
            return Err(fault(401, "Incorrect password"));
        }
        exec(
            c,
            "UPDATE users SET password_hash=? WHERE id=?",
            &[json!(hash_password(new)?), json!(id)],
        )?;
        exec(c, "DELETE FROM sessions WHERE user_id=?", &[json!(id)])?;
        return Ok(Reply {
            body: json!({"status":"password changed"}),
            cookies: vec![session(c, config, id)?],
        });
    }
    Err(fault(404, "Not Found"))
}
