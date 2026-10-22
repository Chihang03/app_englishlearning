//! Subscription compatibility, including key validation and endpoint restrictions.
use super::lexicon::scalar;
use super::*;
use base64::{
    Engine,
    engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD},
};
use openssl::{
    bn::BigNumContext,
    ec::{EcGroup, EcKey, PointConversionForm},
    nid::Nid,
    pkey::PKey,
};
pub fn setting(key: &str) -> String {
    std::env::var(key).unwrap_or_default()
}
pub fn origins() -> Vec<String> {
    setting("WEB_PUSH_ORIGINS")
        .split(',')
        .map(|s| s.trim().trim_end_matches('/').to_owned())
        .filter(|s| !s.is_empty())
        .collect()
}
pub fn config() -> Result<Value> {
    let path = setting("WEB_PUSH_PRIVATE_KEY");
    if path.is_empty() || setting("WEB_PUSH_SUBJECT").is_empty() || origins().is_empty() {
        return Ok(json!({"enabled":false,"public_key":null}));
    }
    let raw = std::fs::read(path).map_err(|_| fault(500, "Push signing key unavailable"))?;
    let key = PKey::private_key_from_pem(&raw)
        .and_then(|k| k.ec_key())
        .map_err(|_| fault(500, "Invalid push signing key"))?;
    if key.group().curve_name() != Some(Nid::X9_62_PRIME256V1) {
        return Err(fault(500, "Invalid push signing curve"));
    }
    let mut context = BigNumContext::new().map_err(|_| fault(500, "Push key allocation"))?;
    let public = key
        .public_key()
        .to_bytes(key.group(), PointConversionForm::UNCOMPRESSED, &mut context)
        .map_err(|_| fault(500, "Push public key unavailable"))?;
    Ok(json!({"enabled":true,"public_key":URL_SAFE_NO_PAD.encode(public)}))
}
pub fn decode(value: &str) -> Result<Vec<u8>> {
    URL_SAFE_NO_PAD
        .decode(value.trim_end_matches('='))
        .or_else(|_| URL_SAFE.decode(value))
        .map_err(|_| fault(422, "Invalid subscription key"))
}
pub fn endpoint(value: &str) -> bool {
    let Ok(url) = url::Url::parse(value) else {
        return false;
    };
    let host = url.host_str().unwrap_or("");
    url.scheme() == "https"
        && (host.ends_with(".push.apple.com")
            || ["fcm.googleapis.com", "updates.push.services.mozilla.com"].contains(&host))
        && url.port_or_known_default() == Some(443)
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
        && !url.path().is_empty()
        && !value.contains('\\')
}
pub fn keys(p: &Value) -> Result<()> {
    let public = decode(s(p, "p256dh"))?;
    if public.len() != 65 || public[0] != 4 || decode(s(p, "auth"))?.len() != 16 {
        return Err(fault(422, "Invalid subscription key"));
    }
    let group = EcGroup::from_curve_name(Nid::X9_62_PRIME256V1)
        .map_err(|_| fault(500, "Push curve unavailable"))?;
    let mut context = BigNumContext::new().map_err(|_| fault(500, "Push key allocation"))?;
    let point = openssl::ec::EcPoint::from_bytes(&group, &public, &mut context)
        .map_err(|_| fault(422, "Invalid P-256 subscription key"))?;
    EcKey::from_public_key(&group, &point)
        .and_then(|k| k.check_key())
        .map_err(|_| fault(422, "Invalid P-256 subscription key"))?;
    Ok(())
}
pub fn handle(c: &Connection, config_: &Config, r: &Request, user: &Value) -> Result<Value> {
    match (r.method.as_str(), r.path.as_str()) {
        ("GET", "/api/push/config") => config(),
        ("POST", "/api/push/subscription") => {
            if !endpoint(s(&r.body, "endpoint")) {
                return Err(fault(422, "Unsupported push service endpoint"));
            }
            keys(&r.body["keys"])?;
            let origin = r.origin.as_deref().unwrap_or("");
            if !origins().iter().any(|v| v == origin) {
                return Err(fault(403, "Notification origin is not allowed"));
            }
            if !b(&config()?, "enabled") {
                return Err(fault(503, "Learning reminders are unavailable"));
            }
            let hash = accounts::token_hash(r.token.as_deref().unwrap_or(""));
            let stamp = json!(config_.stamp());
            if scalar(
                c,
                "SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>?",
                &[json!(hash), user["id"].clone(), stamp.clone()],
            )?
            .is_null()
            {
                return Err(fault(401, "Session expired"));
            }
            let existing = one(
                c,
                "SELECT * FROM push_subscriptions WHERE endpoint=?",
                &[r.body["endpoint"].clone()],
            )?;
            if existing.as_ref().is_some_and(|v| {
                v["p256dh"] != r.body["keys"]["p256dh"] || v["auth"] != r.body["keys"]["auth"]
            }) {
                return Err(fault(409, "Subscription keys do not match"));
            }
            if existing.as_ref().is_none_or(|v| v["user_id"] != user["id"])
                && scalar(
                    c,
                    "SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?",
                    &[user["id"].clone()],
                )?
                .as_i64()
                .unwrap_or(0)
                    >= 10
            {
                return Err(fault(409, "Too many notification devices"));
            }
            exec(
                c,
                "INSERT INTO push_subscriptions(endpoint,user_id,session_hash,origin,p256dh,auth,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,session_hash=excluded.session_hash,origin=excluded.origin,updated_at=excluded.updated_at",
                &[
                    r.body["endpoint"].clone(),
                    user["id"].clone(),
                    json!(hash),
                    json!(origin),
                    r.body["keys"]["p256dh"].clone(),
                    r.body["keys"]["auth"].clone(),
                    stamp.clone(),
                    stamp,
                ],
            )?;
            Ok(json!({"status":"subscribed"}))
        }
        _ => Err(fault(404, "Not Found")),
    }
}
