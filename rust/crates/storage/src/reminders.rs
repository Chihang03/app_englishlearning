//! Native once-per-minute reminder runner. Network I/O never holds a SQLite transaction.
use crate::api::{self, push, *};
use chrono::{DateTime, Datelike, Duration, SecondsFormat, Timelike, Utc};
use fs2::FileExt;
use rusqlite::Connection;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::Path,
    time::{Duration as StdDuration, Instant},
};
#[derive(Default, serde::Serialize, Debug, PartialEq)]
pub struct Counts {
    pub eligible: usize,
    pub sent: usize,
    pub failed: usize,
    pub expired: usize,
}
pub fn open_writer(path: &Path) -> anyhow::Result<Connection> {
    let c = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    c.busy_timeout(StdDuration::from_secs(1))?;
    c.execute_batch("PRAGMA foreign_keys=ON;PRAGMA cache_size=-2048;PRAGMA mmap_size=0;")?;
    anyhow::ensure!(
        c.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))?
            == english_core::SCHEMA_VERSION,
        "Unsupported schema version"
    );
    Ok(c)
}
pub fn message(subscription: &Value, day: &str) -> anyhow::Result<web_push::WebPushMessage> {
    anyhow::ensure!(
        push::endpoint(s(subscription, "endpoint")),
        "Unsupported endpoint"
    );
    let info = web_push::SubscriptionInfo::new(
        s(subscription, "endpoint"),
        s(subscription, "p256dh"),
        s(subscription, "auth"),
    );
    let mut signature = web_push::VapidSignatureBuilder::from_pem(
        std::fs::File::open(push::setting("WEB_PUSH_PRIVATE_KEY"))?,
        &info,
    )?;
    signature.add_claim("sub", push::setting("WEB_PUSH_SUBJECT"));
    let signature = signature.build()?;
    let body=json!({"title":"语境学词","body":"今天还没有学习，花几分钟学几个单词吧。","tag":format!("learning-reminder-{day}"),"url":"/?notification=study#study"}).to_string();
    let mut builder = web_push::WebPushMessageBuilder::new(&info);
    builder.set_ttl(600);
    builder.set_payload(web_push::ContentEncoding::Aes128Gcm, body.as_bytes());
    builder.set_vapid_signature(signature);
    Ok(builder.build()?)
}
pub fn send(subscription: &Value, day: &str) -> std::result::Result<(), Option<u16>> {
    let message = message(subscription, day).map_err(|_| None)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(StdDuration::from_secs(10))
        .connect_timeout(StdDuration::from_secs(5))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| None)?;
    let request = web_push::request_builder::build_request::<Vec<u8>>(message);
    let mut builder = client.post(request.uri().to_string());
    for (k, v) in request.headers() {
        builder = builder.header(k.as_str(), v.as_bytes());
    }
    let response = builder.body(request.into_body()).send().map_err(|_| None)?;
    let status = response.status().as_u16();
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(Some(status))
    }
}
pub fn run(path: &Path, now: DateTime<Utc>, dry: bool) -> anyhow::Result<Counts> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("Database parent missing"))?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .append(true)
        .open(parent.join(".startup.lock"))?;
    if FileExt::try_lock_shared(&lock).is_err() {
        return Ok(Counts::default());
    }
    let timer = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .append(true)
        .open(parent.join(".reminders.lock"))?;
    if timer.try_lock_exclusive().is_err() {
        return Ok(Counts::default());
    }
    if !b(&push::config()?, "enabled") {
        return Ok(Counts::default());
    }
    let mut conn = open_writer(path)?;
    run_with(&mut conn, now, dry, send)
}
pub fn run_with(
    c: &mut Connection,
    now: DateTime<Utc>,
    dry: bool,
    mut sender: impl FnMut(&Value, &str) -> std::result::Result<(), Option<u16>>,
) -> anyhow::Result<Counts> {
    let deadline = Instant::now() + StdDuration::from_secs(45);
    let stamp = now.to_rfc3339_opts(SecondsFormat::Secs, false);
    let previous = (now - Duration::seconds(60)).to_rfc3339_opts(SecondsFormat::Secs, false);
    let mut counts = Counts::default();
    if !dry {
        exec(
            c,
            "DELETE FROM push_deliveries WHERE local_day < ?",
            &[json!((now - Duration::days(32)).date_naive().to_string())],
        )?;
    }
    let mut cursor = String::new();
    loop {
        api::reset_budget();
        let subscriptions = rows(
            c,
            "SELECT p.*,u.timezone FROM push_subscriptions p JOIN users u ON u.id=p.user_id WHERE u.role='learner' AND p.endpoint>? ORDER BY p.endpoint LIMIT 100",
            &[json!(cursor)],
        )?;
        if subscriptions.is_empty() {
            break;
        }
        for subscription in subscriptions {
            cursor = s(&subscription, "endpoint").into();
            if Instant::now() > deadline {
                return Ok(counts);
            }
            let zone = s(&subscription, "timezone")
                .parse::<chrono_tz::Tz>()
                .unwrap_or(chrono_tz::Asia::Shanghai);
            let local = now.with_timezone(&zone);
            if local.hour() != 20 || local.minute() >= 10 {
                continue;
            }
            let day = format!(
                "{:04}-{:02}-{:02}",
                local.year(),
                local.month(),
                local.day()
            );
            let (start, end) = api::study::bounds(local.date_naive(), zone)?;
            let identity = [
                subscription["user_id"].clone(),
                json!(day),
                json!(format!(
                    "{:x}",
                    Sha256::digest(s(&subscription, "endpoint").as_bytes())
                )),
            ];
            c.execute_batch("BEGIN IMMEDIATE")?;
            let claim = (|| -> api::Result<bool> {
                let current = one(
                    c,
                    "SELECT 1 FROM push_subscriptions WHERE endpoint=? AND user_id=? AND updated_at=?",
                    &[
                        subscription["endpoint"].clone(),
                        subscription["user_id"].clone(),
                        subscription["updated_at"].clone(),
                    ],
                )?;
                let learned = one(
                    c,
                    "SELECT 1 FROM review_history WHERE user_id=? AND review_time>=? AND review_time<? LIMIT 1",
                    &[subscription["user_id"].clone(), json!(start), json!(end)],
                )?;
                let delivered = one(
                    c,
                    "SELECT * FROM push_deliveries WHERE user_id=? AND local_day=? AND endpoint_hash=?",
                    &identity,
                )?;
                if current.is_none()
                    || learned.is_some()
                    || delivered.is_some_and(|d| {
                        d["status"] != "failed"
                            || n(&d, "attempts") >= 3
                            || s(&d, "updated_at") > previous.as_str()
                    })
                {
                    return Ok(false);
                }
                counts.eligible += 1;
                if !dry {
                    exec(
                        c,
                        "INSERT INTO push_deliveries(user_id,local_day,endpoint_hash,status,updated_at) VALUES(?,?,?,'claimed',?) ON CONFLICT(user_id,local_day,endpoint_hash) DO UPDATE SET status='claimed',attempts=push_deliveries.attempts+1,updated_at=excluded.updated_at",
                        &[
                            identity[0].clone(),
                            identity[1].clone(),
                            identity[2].clone(),
                            json!(stamp),
                        ],
                    )?;
                }
                Ok(true)
            })();
            let claimed = match claim {
                Ok(v) => {
                    c.execute_batch(if dry { "ROLLBACK" } else { "COMMIT" })?;
                    v
                }
                Err(e) => {
                    let _ = c.execute_batch("ROLLBACK");
                    return Err(e.into());
                }
            };
            if !claimed || dry {
                continue;
            }
            match sender(&subscription, &day) {
                Ok(()) => {
                    exec(
                        c,
                        "UPDATE push_deliveries SET status='sent' WHERE user_id=? AND local_day=? AND endpoint_hash=?",
                        &identity,
                    )?;
                    counts.sent += 1;
                }
                Err(status) => {
                    if matches!(status, Some(404 | 410)) {
                        exec(
                            c,
                            "DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=? AND updated_at=?",
                            &[
                                subscription["endpoint"].clone(),
                                subscription["user_id"].clone(),
                                subscription["updated_at"].clone(),
                            ],
                        )?;
                        counts.expired += 1;
                    }
                    if status.is_some() {
                        exec(
                            c,
                            "UPDATE push_deliveries SET status='failed' WHERE user_id=? AND local_day=? AND endpoint_hash=?",
                            &identity,
                        )?;
                    }
                    counts.failed += 1; /* Never log endpoint capabilities, keys or response bodies. */
                }
            }
        }
    }
    Ok(counts)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn database() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE users(id INTEGER,timezone TEXT,role TEXT);INSERT INTO users VALUES(1,'Asia/Shanghai','learner');CREATE TABLE push_subscriptions(endpoint TEXT,user_id INTEGER,updated_at TEXT);INSERT INTO push_subscriptions VALUES('https://web.push.apple.com/a',1,'2026-10-22T00:00:00+00:00');CREATE TABLE review_history(user_id INTEGER,review_time TEXT);CREATE TABLE push_deliveries(user_id INTEGER,local_day TEXT,endpoint_hash TEXT,status TEXT,attempts INTEGER DEFAULT 1,updated_at TEXT,PRIMARY KEY(user_id,local_day,endpoint_hash));").unwrap();
        c
    }
    #[test]
    fn claim_retry_uncertainty_expiry_dry_run_and_daily_dedup() {
        let now = DateTime::parse_from_rfc3339("2026-10-22T12:00:00+00:00")
            .unwrap()
            .with_timezone(&Utc);
        let mut c = database();
        assert_eq!(
            run_with(&mut c, now, true, |_, _| panic!())
                .unwrap()
                .eligible,
            1
        );
        assert_eq!(
            c.query_row("SELECT COUNT(*) FROM push_deliveries", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            run_with(&mut c, now, false, |_, _| Err(Some(503)))
                .unwrap()
                .failed,
            1
        );
        assert_eq!(
            run_with(&mut c, now + Duration::seconds(30), false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
        assert_eq!(
            run_with(&mut c, now + Duration::minutes(1), false, |_, _| Err(Some(
                503
            )))
            .unwrap()
            .failed,
            1
        );
        assert_eq!(
            run_with(&mut c, now + Duration::minutes(2), false, |_, _| Err(Some(
                503
            )))
            .unwrap()
            .failed,
            1
        );
        assert_eq!(
            run_with(&mut c, now + Duration::minutes(3), false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
        let mut c = database();
        assert_eq!(
            run_with(&mut c, now, false, |_, _| Err(None))
                .unwrap()
                .failed,
            1
        );
        assert_eq!(
            run_with(&mut c, now + Duration::minutes(1), false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
        let mut c = database();
        assert_eq!(
            run_with(&mut c, now, false, |_, _| Err(Some(410)))
                .unwrap()
                .expired,
            1
        );
        assert_eq!(
            c.query_row("SELECT COUNT(*) FROM push_subscriptions", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        let mut c = database();
        assert_eq!(
            run_with(&mut c, now - Duration::seconds(1), false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
        assert_eq!(run_with(&mut c, now, false, |_, _| Ok(())).unwrap().sent, 1);
        assert_eq!(
            run_with(&mut c, now + Duration::minutes(1), false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
        assert_eq!(
            run_with(&mut c, now + Duration::days(1), false, |_, _| Ok(()))
                .unwrap()
                .sent,
            1
        );
        assert_eq!(
            run_with(
                &mut c,
                now + Duration::days(2) + Duration::minutes(10),
                false,
                |_, _| panic!()
            )
            .unwrap()
            .eligible,
            0
        );
        let mut c = database();
        c.execute(
            "INSERT INTO review_history VALUES(1,'2026-10-22T11:59:00+00:00')",
            [],
        )
        .unwrap();
        assert_eq!(
            run_with(&mut c, now, false, |_, _| panic!())
                .unwrap()
                .eligible,
            0
        );
    }
}
