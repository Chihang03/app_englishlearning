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
            c.execute_batch("DROP TABLE IF EXISTS temp.perf_usable; DROP TABLE IF EXISTS temp.perf_core; DROP TABLE IF EXISTS temp.perf_units; DROP TABLE IF EXISTS temp.perf_words; DROP TABLE IF EXISTS temp.perf_revision; PRAGMA temp.cache_size=-2048;")?;
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
    if super::proficiency::adaptive_profile(c, user)?.is_some() {
        return super::proficiency::new_unit(c, user);
    }
    use rusqlite::OptionalExtension;
    let skip: bool = c.query_row("SELECT EXISTS(SELECT 1 FROM settings WHERE user_id=? AND key='skip_basic_600' AND value='true')",[user],|r|r.get(0))?;
    Ok(c.prepare_cached(include_str!("new_unit.sql"))?
        .query_row(rusqlite::named_params! {":user":user,":skip":skip}, |r| {
            Ok(json!({"learning_unit_id":r.get::<_,i64>(0)?}))
        })
        .optional()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalogue() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        let schema: Vec<String> =
            serde_json::from_str(include_str!("../../../../fixtures/schema-v22.json")).unwrap();
        for statement in schema {
            c.execute_batch(&statement).unwrap();
        }
        c.pragma_update(None, "foreign_keys", false).unwrap();
        // "bank": two bilingual homograph entries plus a monolingual duplicate source.
        c.execute_batch(
            "INSERT INTO users(id,username,password_hash,timezone,created_at) VALUES(1,'u','x','UTC','2026-01-01');
             INSERT INTO meaning_catalog_revision VALUES(1,'test',1);
             INSERT INTO words(id,word,part_of_speech,definition_cn,example_sentence) VALUES(1,'bank','n','岸','x'),(2,'cat','n','猫','x');
             INSERT INTO word_frequencies VALUES('bank',500,'a'),('cat',400,'b');
             INSERT INTO lexical_units(id,word_id,unit_key,headword,normalized_headword,pos_group,identity_status) VALUES(1,1,'bank:n','bank','bank','noun','legacy'),(2,2,'cat:n','cat','cat','noun','legacy');
             INSERT INTO word_senses(id,word_id,sense_key,part_of_speech,definition_cn,source,position,lexical_unit_id) VALUES
              (1,1,'zh:e1.002','名词','岸','d',0,1),(2,1,'zh:e1.003','名词','堆','d',1,1),(3,1,'zh:e1.006','名词','大量','d',2,1),
              (4,1,'zh:e2.002','名词','银行','d',3,1),(5,1,'en:x1.015','名词','倾斜','d',4,1),(6,2,'en:c1.001','名词','猫','d',0,2);
             INSERT INTO sense_examples(id,sense_id,sentence,target_form,source) SELECT id,id,'s'||id,'t','d' FROM word_senses;
             INSERT INTO learning_units(id,lexical_unit_id,word_id,unit_key,representative_sense_id,display_definition_cn,position,verification)
              SELECT id,lexical_unit_id,word_id,'sense:'||id,id,definition_cn,position,'legacy' FROM word_senses;
             INSERT INTO learning_unit_senses SELECT id,id FROM word_senses;
             INSERT INTO vocabulary_lists(list_id,title,description,source_url) VALUES('l','L','','');
             INSERT INTO word_list_learning_units(list_id,learning_unit_id,position,verification) SELECT 'l',id,0,'legacy' FROM learning_units;
             INSERT INTO user_vocabulary_lists VALUES(1,'l',1);",
        )
        .unwrap();
        super::super::readiness::prepare(&c, &Config::from_env(None), 1).unwrap();
        ensure_content(&c).unwrap();
        c
    }

    fn learn(c: &Connection) -> i64 {
        let unit = new_unit(c, 1).unwrap().unwrap()["learning_unit_id"]
            .as_i64()
            .unwrap();
        c.execute(
            "INSERT INTO sense_srs_state(user_id,sense_id,next_review_date,status,learning_unit_id) VALUES(1,?1,'2099-01-01','Learning',?1)",
            [unit],
        )
        .unwrap();
        unit
    }

    fn new_senses(c: &Connection) -> i64 {
        metrics(c, 1, "2026-01-01", "2026-01-01T00:00:00+00:00").unwrap()["new_senses"]
            .as_i64()
            .unwrap()
    }

    #[test]
    fn core_senses_come_first_and_bound_the_new_scope() {
        let c = catalogue();
        let core: Vec<i64> = c
            .prepare("SELECT id FROM temp.perf_units WHERE core ORDER BY id")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        // First two senses per entry of the primary source; the homograph keeps its own first sense.
        assert_eq!(core, [1, 2, 4, 6]);
        assert_eq!(new_senses(&c), 4);
        // Untouched words first, then the remaining core senses before any extended sense.
        assert_eq!([learn(&c), learn(&c), learn(&c)], [1, 6, 2]);
        assert_eq!(new_senses(&c), 1);
        assert_eq!(learn(&c), 4);
        // Only once every core sense has been started do extended senses become new content.
        assert_eq!(new_senses(&c), 2);
        assert_eq!([learn(&c), learn(&c)], [3, 5]);
        assert!(new_unit(&c, 1).unwrap().is_none());
    }
}
