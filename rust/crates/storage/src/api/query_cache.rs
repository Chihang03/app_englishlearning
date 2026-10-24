//! Derived, connection-local content indexes. Learner writes never invalidate them.
use super::*;

// v22's catalogue revision does not cover unit.active/word_id or
// sense.learning_enabled. Hash these narrow columns after database writes;
// definitions/examples still use the existing content revision. No user data
// is hashed, and ordinary repeated reads do not scan these columns again.
fn flag_signature(c: &Connection) -> rusqlite::Result<String> {
    use sha2::{Digest, Sha256};
    let mut hash = Sha256::new();
    for query in [
        "SELECT id,word_id,active FROM learning_units ORDER BY id",
        "SELECT id,learning_enabled FROM word_senses ORDER BY id",
    ] {
        hash.update(query.as_bytes());
        let mut statement = c.prepare_cached(query)?;
        let columns = statement.column_count();
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            for column in 0..columns {
                hash.update(row.get::<_, i64>(column)?.to_le_bytes());
            }
        }
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub fn ensure_content(c: &Connection) -> Result<()> {
    let revision: (String, i64) = c.query_row(
        "SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let data_version: i64 = c.pragma_query_value(None, "data_version", |r| r.get(0))?;
    let changes: i64 = c.query_row("SELECT total_changes()", [], |r| r.get(0))?;
    let saved = c
        .query_row(
            "SELECT database_id,revision,flags,data_version,changes FROM temp.perf_revision",
            [],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            },
        )
        .ok();
    if saved.as_ref().is_some_and(|v| {
        v.0 == revision.0 && v.1 == revision.1 && v.3 == data_version && v.4 == changes
    }) {
        return Ok(());
    }
    let flags = flag_signature(c)?;
    let rebuild = saved
        .as_ref()
        .is_none_or(|v| v.0 != revision.0 || v.1 != revision.1 || v.2 != flags);
    let query_only: bool = c.pragma_query_value(None, "query_only", |r| r.get(0))?;
    // Read-only main database handles remain read-only even while writing TEMP.
    c.pragma_update(None, "query_only", false)?;
    let result = (|| -> rusqlite::Result<()> {
        c.execute_batch("SAVEPOINT content_index")?;
        if rebuild {
            c.execute_batch("DROP TABLE IF EXISTS temp.perf_usable; DROP TABLE IF EXISTS temp.perf_units; DROP TABLE IF EXISTS temp.perf_words; DROP TABLE IF EXISTS temp.perf_revision; PRAGMA temp.cache_size=-2048;")?;
            c.execute_batch(include_str!("content_index.sql"))?;
        } else {
            c.execute("DELETE FROM temp.perf_revision", [])?;
        }
        let before: i64 = c.query_row("SELECT total_changes()", [], |r| r.get(0))?;
        c.execute(
            "INSERT INTO temp.perf_revision VALUES(?,?,?,?,?)",
            (&revision.0, revision.1, &flags, data_version, before + 1),
        )?;
        c.execute_batch("RELEASE content_index")?;
        Ok(())
    })();
    if result.is_err() {
        let _ = c.execute_batch("ROLLBACK TO content_index; RELEASE content_index");
    }
    c.pragma_update(None, "query_only", query_only)?;
    result?;
    Ok(())
}

pub fn metrics(c: &Connection, user: i64, today: &str, now: &str) -> Result<Value> {
    let skip: bool = c.query_row("SELECT EXISTS(SELECT 1 FROM settings WHERE user_id=? AND key='skip_basic_600' AND value='true')", [user], |r| r.get(0))?;
    let mut statement = c.prepare_cached(include_str!("metrics.sql"))?;
    let names = statement
        .column_names()
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    let result = statement.query_row(
        rusqlite::named_params! {":user":user,":today":today,":now":now,":skip":skip},
        |r| {
            let mut result = Map::new();
            for (i, name) in names.iter().enumerate() {
                let value = match r.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(n) => json!(n),
                    ValueRef::Text(s) => json!(String::from_utf8_lossy(s)),
                    _ => return Err(rusqlite::Error::InvalidQuery),
                };
                result.insert(name.clone(), value);
            }
            Ok(Value::Object(result))
        },
    )?;
    Ok(result)
}

pub fn note_learning_write(c: &Connection) -> Result<()> {
    c.execute(
        "UPDATE temp.perf_revision SET changes=total_changes()+1",
        [],
    )?;
    Ok(())
}

pub fn new_unit(c: &Connection, user: i64) -> Result<Option<Value>> {
    use rusqlite::OptionalExtension;
    let skip: bool = c.query_row("SELECT EXISTS(SELECT 1 FROM settings WHERE user_id=? AND key='skip_basic_600' AND value='true')",[user],|r|r.get(0))?;
    Ok(c.prepare_cached(include_str!("new_unit.sql"))?
        .query_row(rusqlite::named_params! {":user":user,":skip":skip}, |r| {
            Ok(json!({"learning_unit_id":r.get::<_,i64>(0)?}))
        })
        .optional()?)
}
