//! Native API use cases. A call owns one transaction and cannot partially commit on a domain error.
pub mod accounts;
pub mod admin;
pub mod forms;
pub mod lexicon;
pub mod passkeys;
pub mod push;
mod query_cache;
pub mod study;
pub mod validation;
use chrono::{DateTime, SecondsFormat, Utc};
use rusqlite::{
    Connection, params_from_iter,
    types::{Value as SqlValue, ValueRef},
};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
thread_local! {static RESULT_BYTES:std::cell::Cell<usize>=const{std::cell::Cell::new(0)};}
pub fn reset_budget() {
    RESULT_BYTES.with(|budget| budget.set(0));
}
pub fn fold(value: &str) -> String {
    use unicode_casefold::UnicodeCaseFold;
    value.case_fold().collect()
}

#[derive(Debug, thiserror::Error)]
#[error("{detail}")]
pub struct Fault {
    pub status: u16,
    pub detail: String,
    pub validation: Option<Value>,
}
impl From<rusqlite::Error> for Fault {
    fn from(e: rusqlite::Error) -> Self {
        let busy = matches!(e,rusqlite::Error::SqliteFailure(ref code,_) if matches!(code.code,rusqlite::ErrorCode::DatabaseBusy|rusqlite::ErrorCode::DatabaseLocked|rusqlite::ErrorCode::OperationInterrupted));
        Self {
            status: if busy { 503 } else { 500 },
            detail: if busy {
                "Database busy".into()
            } else {
                e.to_string()
            },
            validation: None,
        }
    }
}
impl From<anyhow::Error> for Fault {
    fn from(e: anyhow::Error) -> Self {
        Self {
            status: 500,
            detail: e.to_string(),
            validation: None,
        }
    }
}
pub type Result<T> = std::result::Result<T, Fault>;
pub fn fault(status: u16, detail: impl Into<String>) -> Fault {
    Fault {
        status,
        detail: detail.into(),
        validation: None,
    }
}

#[derive(Clone)]
pub struct Config {
    pub cookie_secure: bool,
    pub registration_code: String,
    pub default_timezone: String,
    pub clock: Option<DateTime<Utc>>,
    pub rp_id: String,
    pub rp_name: String,
    pub origins: Vec<String>,
}
impl Config {
    pub fn from_env(clock: Option<DateTime<Utc>>) -> Self {
        Self {
            cookie_secure: std::env::var("COOKIE_SECURE")
                .is_ok_and(|v| matches!(v.as_str(), "1" | "true" | "yes")),
            registration_code: std::env::var("REGISTRATION_CODE").unwrap_or_default(),
            default_timezone: std::env::var("DEFAULT_TIMEZONE")
                .unwrap_or_else(|_| "Asia/Shanghai".into()),
            clock,
            rp_id: std::env::var("WEBAUTHN_RP_ID")
                .unwrap_or_else(|_| "localhost".into())
                .trim()
                .to_ascii_lowercase(),
            rp_name: std::env::var("WEBAUTHN_RP_NAME")
                .unwrap_or_else(|_| "Context Vocabulary Trainer".into()),
            origins: std::env::var("WEBAUTHN_ORIGINS")
                .unwrap_or_else(|_| "http://localhost:5173,http://localhost:8000".into())
                .split(',')
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
                .collect(),
        }
    }

    pub fn now(&self) -> DateTime<Utc> {
        self.clock.unwrap_or_else(Utc::now)
    }
    pub fn stamp(&self) -> String {
        self.now().to_rfc3339_opts(SecondsFormat::Secs, false)
    }
}
#[derive(Clone)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: BTreeMap<String, String>,
    pub body: Value,
    pub token: Option<String>,
    pub origin: Option<String>,
    pub cookies: BTreeMap<String, String>,
}
pub struct Reply {
    pub body: Value,
    pub cookies: Vec<String>,
}
impl From<Value> for Reply {
    fn from(body: Value) -> Self {
        Self {
            body,
            cookies: Vec::new(),
        }
    }
}

pub fn values(items: &[Value]) -> Vec<SqlValue> {
    items
        .iter()
        .map(|v| match v {
            Value::Null => SqlValue::Null,
            Value::String(v) => SqlValue::Text(v.clone()),
            Value::Bool(v) => SqlValue::Integer(i64::from(*v)),
            Value::Number(v) => v.as_i64().map_or_else(
                || SqlValue::Real(v.as_f64().unwrap_or(0.0)),
                SqlValue::Integer,
            ),
            _ => SqlValue::Text(v.to_string()),
        })
        .collect()
}
pub fn rows(c: &Connection, sql: &str, args: &[Value]) -> Result<Vec<Value>> {
    let mut s = c.prepare(sql)?;
    let names = s
        .column_names()
        .iter()
        .map(|n| n.to_string())
        .collect::<Vec<_>>();
    let mapped = s.query_map(params_from_iter(values(args)), |r| {
        let mut result = Map::new();
        for (i, name) in names.iter().enumerate() {
            let value = match r.get_ref(i)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(n) => json!(n),
                ValueRef::Real(n) => json!(n),
                ValueRef::Text(v) => json!(String::from_utf8_lossy(v)),
                ValueRef::Blob(v) => json!(v),
            };
            result.insert(name.clone(), value);
        }
        Ok(Value::Object(result))
    })?;
    let mut result = Vec::new();
    for item in mapped {
        let item = item?;
        let bytes = serde_json::to_vec(&item)
            .map_err(|_| fault(500, "Serialize database row"))?
            .len();
        let total = RESULT_BYTES.with(|budget| {
            let total = budget.get().saturating_add(bytes);
            budget.set(total);
            total
        });
        if total > 8 * 1024 * 1024 {
            return Err(fault(503, "Request data budget exceeded"));
        }
        result.push(item);
    }
    Ok(result)
}
pub fn one(c: &Connection, sql: &str, args: &[Value]) -> Result<Option<Value>> {
    Ok(rows(c, sql, args)?.into_iter().next())
}
pub fn exec(c: &Connection, sql: &str, args: &[Value]) -> Result<usize> {
    Ok(c.execute(sql, params_from_iter(values(args)))?)
}
pub fn n(v: &Value, key: &str) -> i64 {
    v[key].as_i64().unwrap_or(0)
}
pub fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn b(v: &Value, key: &str) -> bool {
    v[key].as_bool().unwrap_or_else(|| n(v, key) != 0)
}
pub fn integer(v: &Value, key: &str) -> Result<i64> {
    v[key]
        .as_i64()
        .or_else(|| v[key].as_str().and_then(|s| s.parse().ok()))
        .ok_or_else(|| fault(422, format!("{key}: Input should be a valid integer")))
}
pub fn string<'a>(v: &'a Value, key: &str, min: usize, max: usize) -> Result<&'a str> {
    let text = v[key]
        .as_str()
        .ok_or_else(|| fault(422, format!("{key}: Input should be a valid string")))?;
    if !(min..=max).contains(&text.chars().count()) {
        return Err(fault(
            422,
            format!("{key}: String length outside allowed range"),
        ));
    }
    Ok(text)
}

/// Warm derived TEMP indexes before accepting HTTP traffic.
pub fn prepare_content(c: &Connection) -> Result<()> {
    c.execute_batch("BEGIN")?;
    let result = query_cache::ensure_content(c);
    if result.is_ok() {
        c.execute_batch("COMMIT")?;
    } else {
        let _ = c.execute_batch("ROLLBACK");
    }
    result
}

fn learning_write(request: &Request) -> bool {
    request.path == "/api/next"
        || request.method == "POST"
            && (request.path == "/api/review" || request.path.starts_with("/api/study/"))
}

pub fn handle(c: &mut Connection, config: &Config, request: &Request) -> Result<Reply> {
    reset_budget();
    static ROUTES: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    let routes = ROUTES.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../fixtures/routes.json"))
            .expect("frozen routes")
    });
    let matches = routes
        .as_array()
        .unwrap()
        .iter()
        .filter(|route| {
            let a = s(route, "path").split('/').collect::<Vec<_>>();
            let b = request.path.split('/').collect::<Vec<_>>();
            a.len() == b.len()
                && a.iter()
                    .zip(b)
                    .all(|(a, b)| (*a == b) || a.starts_with('{') && !b.is_empty())
        })
        .collect::<Vec<_>>();
    if matches.is_empty() {
        return Err(fault(404, "Not Found"));
    }
    if !matches.iter().any(|r| r["method"] == request.method) {
        return Err(fault(405, "Method Not Allowed"));
    }
    let retired = request.path == "/api/words" || request.path == "/api/words/import";
    let public = matches!(
        request.path.as_str(),
        "/api/auth/register"
            | "/api/auth/login"
            | "/api/auth/logout"
            | "/api/auth/passkeys/config"
            | "/api/auth/passkeys/login/options"
            | "/api/auth/passkeys/login/verify"
    );
    if !public && !retired {
        let user = accounts::authenticate(c, config, request)?;
        if request.path.starts_with("/api/admin/") {
            if s(&user, "role") != "admin" {
                return Err(fault(403, "Administrator access required"));
            }
        } else if !request.path.starts_with("/api/auth/") && s(&user, "role") != "learner" {
            return Err(fault(
                403,
                "Learning is unavailable for administrator accounts",
            ));
        }
    }
    let mut normalized = request.clone();
    normalized.body = validation::normalize(&request.path, &request.method, &request.body)?;
    let request = &normalized;
    if request.path == "/api/admin/report-review/import" {
        return admin::import(c, config, request);
    }
    if request.path.starts_with("/api/auth/passkeys") {
        let result = passkeys::handle(c, config, request);
        if !c.is_autocommit() {
            let _ = c.execute_batch("ROLLBACK");
        }
        return result;
    }
    if request.path == "/api/words" || request.path == "/api/words/import" {
        return Err(fault(410, "用户自导入功能已停用"));
    }
    let writes = request.method != "GET" || request.path == "/api/next";
    c.execute_batch(if writes { "BEGIN IMMEDIATE" } else { "BEGIN" })?;
    let result = (|| {
        if learning_write(request)
            || matches!(
                request.path.as_str(),
                "/api/stats" | "/api/word-lists" | "/api/next"
            )
        {
            query_cache::ensure_content(c)?;
        }
        if request.path.starts_with("/api/auth/") {
            return accounts::handle(c, config, request);
        }
        let user = accounts::authenticate(c, config, request)?;
        if request.path.starts_with("/api/admin/") {
            return admin::handle(c, config, request, &user).map(Into::into);
        }
        if s(&user, "role") != "learner" {
            return Err(fault(
                403,
                "Learning is unavailable for administrator accounts",
            ));
        }
        if matches!(
            request.path.as_str(),
            "/api/next"
                | "/api/review"
                | "/api/study/hint"
                | "/api/study/related-exposure"
                | "/api/study/meanings"
                | "/api/content-reports"
                | "/api/stats"
                | "/api/learning-calendar"
        ) {
            return study::handle(c, config, request, &user).map(Into::into);
        }
        if request.path.starts_with("/api/push/") {
            return push::handle(c, config, request, &user).map(Into::into);
        }
        lexicon::handle(c, config, request, &user).map(Into::into)
    })();
    match result {
        Ok(reply) => {
            if learning_write(request) {
                // These handlers only change learner state. Their own committed
                // changes need no content fingerprint scan on the next card.
                query_cache::note_learning_write(c)?;
            }
            c.execute_batch("COMMIT")?;
            Ok(reply)
        }
        Err(error) => {
            let _ = c.execute_batch("ROLLBACK");
            Err(error)
        }
    }
}
