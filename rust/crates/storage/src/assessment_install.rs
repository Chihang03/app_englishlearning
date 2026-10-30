//! Explicit, backed-up additive migration. HTTP never alters the schema.
use anyhow::{Result, ensure};
use rusqlite::Connection;
use serde_json::{Value, json};
use std::path::Path;
pub fn installed(c: &Connection) -> bool {
    c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='assessment_schema')",
        [],
        |r| r.get(0),
    )
    .unwrap_or(false)
}
pub fn install(database: &Path, backup: &Path) -> Result<Value> {
    // backup() refuses overwrite, verifies main schema and includes the WAL.
    crate::operations::backup(database, backup)?;
    let mut c = Connection::open(database)?;
    c.busy_timeout(std::time::Duration::from_secs(5))?;
    c.pragma_update(None, "foreign_keys", true)?;
    let version: i64 = c.pragma_query_value(None, "user_version", |r| r.get(0))?;
    ensure!(version == 22, "Expected v22 database");
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute_batch(include_str!("assessment_schema.sql"))?;
    let bank: Value = serde_json::from_str(include_str!("../../../fixtures/assessment-bank.json"))?;
    for item in bank["items"].as_array().unwrap() {
        let id = item["id"].as_str().unwrap();
        // IDs are immutable: an edited item must get a new ID/version.
        let payload = serde_json::to_string(item)?;
        let old: Option<String> = tx
            .query_row(
                "SELECT payload FROM assessment_items WHERE id=?",
                [id],
                |r| r.get(0),
            )
            .optional()?;
        ensure!(
            old.as_ref().is_none_or(|p| p == &payload),
            "Immutable item changed: {id}"
        );
        tx.execute("INSERT OR IGNORE INTO assessment_items(id,version,word,family,kind,band,difficulty,payload) VALUES(?,?,?,?,?,?,?,?)",
            rusqlite::params![id,bank["version"].as_str(),item["word"].as_str(),item["family"].as_str(),item["kind"].as_str(),item["band"].as_i64(),item["difficulty"].as_f64(),payload])?;
    }
    // Bind only exact authored meanings. Other senses at the same spelling
    // retain an unverified frequency prior instead of inheriting a level.
    tx.execute_batch("INSERT OR IGNORE INTO learning_unit_difficulty(learning_unit_id,difficulty,source,verified) SELECT u.id,i.difficulty,'authored-exact-meaning',0 FROM learning_units u JOIN words w ON w.id=u.word_id JOIN assessment_items i ON i.word=lower(trim(w.word)) AND i.kind='recognition' WHERE u.active=1 AND trim(u.display_definition_cn)=json_extract(i.payload,'$.meaning')")?;
    tx.commit()?;
    Ok(
        json!({"extension":1,"items":bank["items"].as_array().unwrap().len(),"backup":backup,"main_schema":22}),
    )
}
use rusqlite::OptionalExtension;
