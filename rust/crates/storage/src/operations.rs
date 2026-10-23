//! Explicit production maintenance. HTTP startup never creates or migrates a database.
use crate::api::{self, lexicon::scalar, *};
use anyhow::{Context, ensure};
use rusqlite::Connection;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::{fs::File, path::Path};
pub fn initialize(path: &Path) -> anyhow::Result<Value> {
    ensure!(
        !path.exists(),
        "Database already exists; source left unchanged"
    );
    let parent = path.parent().context("Database parent missing")?;
    ensure!(parent.is_dir(), "Create the data directory first");
    let temp = tempfile::NamedTempFile::new_in(parent)?;
    {
        let mut c = Connection::open(temp.path())?;
        let tx = c.transaction()?;
        let statements: Vec<String> =
            serde_json::from_str(include_str!("../../../fixtures/schema-v22.json"))?;
        for statement in statements {
            tx.execute_batch(&statement)?;
        }
        tx.execute("INSERT INTO meaning_catalog_revision(id,database_id) VALUES(1,lower(hex(randomblob(16))))",[])?;
        tx.execute_batch("PRAGMA user_version=22")?;
        tx.commit()?;
        ensure!(
            c.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))? == "ok",
            "Invalid new schema"
        );
    }
    temp.as_file().sync_all()?;
    temp.persist_noclobber(path)
        .context("Publish initialized database")?;
    File::open(parent)?.sync_all()?;
    let c = crate::reminders::open_writer(path)?;
    c.execute_batch("PRAGMA journal_mode=WAL;")?;
    inspect(path)
}
pub fn inspect(path: &Path) -> anyhow::Result<Value> {
    let c = crate::open_readonly(path)?;
    let check: String = c.query_row("PRAGMA quick_check", [], |r| r.get(0))?;
    ensure!(check == "ok", "Database quick_check failed");
    ensure!(
        rows(&c, "PRAGMA foreign_key_check", &[])?.is_empty(),
        "Foreign key violations"
    );
    Ok(
        json!({"schema_version":english_core::SCHEMA_VERSION,"catalog":one(&c,"SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",&[])?,"users":scalar(&c,"SELECT COUNT(*) FROM users",&[])?,"words":scalar(&c,"SELECT COUNT(*) FROM words",&[])?,"reviews":scalar(&c,"SELECT COUNT(*) FROM review_history",&[])?,"integrity":"ok"}),
    )
}
pub fn backup(source: &Path, target: &Path) -> anyhow::Result<Value> {
    ensure!(!target.exists(), "Backup target already exists");
    let c = crate::open_readonly(source)?;
    let parent = target.parent().context("Backup parent missing")?;
    let temporary = tempfile::NamedTempFile::new_in(parent)?;
    c.backup("main", temporary.path(), None)?;
    temporary.as_file().sync_all()?;
    inspect(temporary.path())?;
    temporary.persist_noclobber(target)?;
    File::open(parent)?.sync_all()?;
    Ok(json!({"status":"backed_up","database":target}))
}
pub fn fix_study_examples(path: &Path, dry_run: bool) -> anyhow::Result<Value> {
    let mut c = crate::reminders::open_writer(path)?;
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let choices = rows(
        &tx,
        include_str!("../../../../backend/app/fixed_example_choices.sql"),
        &[],
    )?;
    let changed = choices
        .iter()
        .filter(|r| r["example_id"] != r["previous_example_id"])
        .count();
    for row in &choices {
        exec(
            &tx,
            "UPDATE user_sense_examples SET retired_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL AND sense_id!=?",
            &[
                row["user_id"].clone(),
                row["learning_unit_id"].clone(),
                row["sense_id"].clone(),
            ],
        )?;
        exec(
            &tx,
            "INSERT INTO user_sense_examples(user_id,sense_id,example_id,learning_unit_id,retired_at) VALUES(?,?,?,?,NULL) ON CONFLICT(user_id,sense_id) DO UPDATE SET example_id=excluded.example_id,learning_unit_id=excluded.learning_unit_id,retired_at=NULL",
            &[
                row["user_id"].clone(),
                row["sense_id"].clone(),
                row["example_id"].clone(),
                row["learning_unit_id"].clone(),
            ],
        )?;
    }
    let units = tx.execute(
        "UPDATE learning_units SET example_strategy='fixed' WHERE example_strategy='rotate'",
        [],
    )?;
    let result = json!({"units_fixed":units,"cards_checked":choices.len(),"cards_changed":changed,"dry_run":dry_run});
    if !dry_run {
        tx.commit()?;
    }
    Ok(result)
}
pub fn create_admin(path: &Path, password: &str, config: &api::Config) -> anyhow::Result<Value> {
    ensure!(
        (1..=128).contains(&password.chars().count()),
        "Password must contain 1–128 characters"
    );
    let mut c = crate::reminders::open_writer(path)?;
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let existing = rows(
        &tx,
        "SELECT role FROM users WHERE lower(username)='admin'",
        &[],
    )?;
    if !existing.is_empty() {
        ensure!(
            existing.len() == 1 && existing[0]["role"] == "admin",
            "A learner already uses this name; existing account was left unchanged"
        );
        return Ok(json!({"created":false}));
    }
    exec(
        &tx,
        "INSERT INTO users(username,password_hash,timezone,created_at,role) VALUES('admin',?,?,?,'admin')",
        &[
            json!(accounts::hash_password(password)?),
            json!(config.default_timezone),
            json!(config.stamp()),
        ],
    )?;
    tx.commit()?;
    Ok(json!({"created":true}))
}
pub fn report(
    path: &Path,
    admin_id: i64,
    action: &str,
    input: Option<Value>,
    dry: bool,
    limit: i64,
    after: i64,
) -> anyhow::Result<Value> {
    let mut c = if action == "import" && !dry {
        crate::reminders::open_writer(path)?
    } else {
        crate::open_readonly(path)?
    };
    let user = one(
        &c,
        "SELECT id,role FROM users WHERE id=? AND role='admin'",
        &[json!(admin_id)],
    )?
    .context("An existing administrator ID is required")?;
    let config = api::Config::from_env(None);
    let mut r = api::Request {
        method: if action == "export" { "GET" } else { "POST" }.into(),
        path: format!("/api/admin/report-review/{action}"),
        body: input.unwrap_or(json!({})),
        token: None,
        origin: None,
        cookies: Default::default(),
        query: BTreeMap::from([
            ("dry_run".into(), dry.to_string()),
            ("limit".into(), limit.to_string()),
            ("after_id".into(), after.to_string()),
        ]),
    };
    r.body = api::validation::normalize(&r.path, &r.method, &r.body)?;
    if action == "import" {
        Ok(api::admin::import_as(&mut c, &config, &r, &user)?.body)
    } else {
        Ok(api::admin::handle(&c, &config, &r, &user)?)
    }
}
