//! Shared eligibility for selection, reviews, stats and reminders. No invented SRS.
use super::lexicon::scalar;
use super::*;
use chrono::Duration;
pub fn installed(c: &Connection) -> bool {
    c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='readiness_schema')",
        [],
        |r| r.get(0),
    )
    .unwrap_or(false)
}
pub fn prepare(c: &Connection, config: &Config, uid: i64) -> Result<()> {
    let before: i64 = c.query_row("SELECT total_changes()", [], |r| r.get(0))?;
    let only: bool = c.pragma_query_value(None, "query_only", |r| r.get(0))?;
    c.pragma_update(None, "query_only", false)?;
    let result = (|| -> Result<()> {
        c.execute_batch("CREATE TEMP TABLE IF NOT EXISTS policy_excluded(id INTEGER PRIMARY KEY); CREATE TEMP TABLE IF NOT EXISTS policy_excluded_words(id INTEGER PRIMARY KEY); DELETE FROM temp.policy_excluded_words; DELETE FROM temp.policy_excluded;")?;
        if installed(c) {
            let basic =
                super::lexicon::sql("basic_unit").replace("=binding.learning_unit_id", "=u.id");
            let query="INSERT INTO temp.policy_excluded SELECT r.learning_unit_id AS id FROM main.user_unit_readiness r JOIN main.readiness_details d USING(user_id,learning_unit_id) JOIN main.user_proficiency_profiles p USING(user_id) JOIN main.learning_units u ON u.id=r.learning_unit_id JOIN main.words w ON w.id=u.word_id WHERE r.user_id=:uid AND p.mode='adaptive' AND r.state='deferred' AND d.source='scope_confirmation' AND NOT EXISTS(SELECT 1 FROM main.sense_srs_state scheduled WHERE scheduled.user_id=r.user_id AND scheduled.learning_unit_id=u.id AND scheduled.retired_at IS NULL) AND d.valid_until>:stamp AND u.active=1 AND (w.owner_id IS NULL OR w.owner_id=:uid) AND d.definition_cn=u.display_definition_cn AND d.lexical_unit_id=u.lexical_unit_id AND NOT EXISTS(SELECT 1 FROM main.readiness_overrides o WHERE o.user_id=r.user_id AND o.learning_unit_id=r.learning_unit_id) AND NOT EXISTS(SELECT 1 FROM main.review_history h WHERE h.user_id=r.user_id AND h.learning_unit_id=r.learning_unit_id AND h.is_first_attempt=1 AND h.is_correct=0 AND h.review_time>=CASE WHEN d.source='personal_confirmation' THEN d.source_id ELSE r.updated_at END) AND NOT EXISTS(SELECT 1 FROM main.user_muted_words muted WHERE muted.user_id=:uid AND muted.word=lower(trim(w.word))) AND NOT (EXISTS(SELECT 1 FROM main.settings bs WHERE bs.user_id=:uid AND bs.key='skip_basic_600' AND bs.value='true') AND __BASIC__);".replace("__BASIC__",&basic);
            c.prepare_cached(&query)?
                .execute(rusqlite::named_params! {":uid":uid,":stamp":config.stamp()})?;
        }
        c.execute_batch("INSERT INTO temp.policy_excluded_words SELECT DISTINCT u.word_id AS id FROM temp.policy_excluded e CROSS JOIN main.learning_units u ON u.id=e.id WHERE NOT EXISTS(SELECT 1 FROM main.learning_units other WHERE other.word_id=u.word_id AND other.active=1 AND NOT EXISTS(SELECT 1 FROM temp.policy_excluded x WHERE x.id=other.id) AND EXISTS(SELECT 1 FROM main.learning_unit_senses b JOIN main.word_senses s ON s.id=b.sense_id JOIN main.sense_examples ex ON ex.sense_id=s.id WHERE b.learning_unit_id=other.id AND s.active=1 AND s.learning_enabled=1 AND ex.active=1));")?;
        Ok(())
    })();
    if result.is_ok()
        && c.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_temp_master WHERE name='perf_revision')",
            [],
            |r| r.get::<_, bool>(0),
        )?
    {
        c.execute(
            "UPDATE temp.perf_revision SET changes=total_changes()+1 WHERE changes=?",
            [before],
        )?;
    }
    c.pragma_update(None, "query_only", only)?;
    result
}

fn defer(
    c: &Connection,
    config: &Config,
    uid: i64,
    unit: i64,
    source: &str,
    source_id: &str,
    days: i64,
) -> Result<()> {
    let evidence_at = if source == "personal_confirmation" {
        chrono::DateTime::parse_from_rfc3339(source_id)
            .ok()
            .map(|t| t.with_timezone(&Utc))
    } else {
        one(
            c,
            "SELECT completed_at FROM assessment_sessions WHERE id=?",
            &[json!(source_id)],
        )?
        .and_then(|v| {
            chrono::DateTime::parse_from_rfc3339(s(&v, "completed_at"))
                .ok()
                .map(|t| t.with_timezone(&Utc))
        })
    };
    let Some(at) = evidence_at else {
        return Ok(());
    };
    let cap = at
        + Duration::days(if source == "personal_confirmation" {
            30
        } else {
            14
        });
    let until = std::cmp::min(config.now() + Duration::days(days), cap);
    if until <= config.now() {
        return Ok(());
    }
    let revision = scalar(
        c,
        "SELECT revision FROM user_proficiency_profiles WHERE user_id=?",
        &[json!(uid)],
    )?;
    let evidence = json!({"policy":"scope-readiness-v3","source":source,"source_id":source_id});
    exec(
        c,
        "INSERT INTO user_unit_readiness(user_id,learning_unit_id,state,evidence,profile_revision,updated_at) VALUES(?,?,'deferred',?,?,?) ON CONFLICT(user_id,learning_unit_id) DO UPDATE SET state='deferred',evidence=excluded.evidence,profile_revision=excluded.profile_revision,updated_at=excluded.updated_at",
        &[
            json!(uid),
            json!(unit),
            json!(evidence.to_string()),
            revision,
            json!(config.stamp()),
        ],
    )?;
    let until = until.to_rfc3339_opts(SecondsFormat::Secs, false);
    exec(
        c,
        "INSERT INTO readiness_details(user_id,learning_unit_id,source,source_id,valid_until,definition_cn,lexical_unit_id) SELECT ?,id,?,?,?,display_definition_cn,lexical_unit_id FROM learning_units WHERE id=? ON CONFLICT(user_id,learning_unit_id) DO UPDATE SET source=excluded.source,source_id=excluded.source_id,valid_until=excluded.valid_until,definition_cn=excluded.definition_cn,lexical_unit_id=excluded.lexical_unit_id",
        &[
            json!(uid),
            json!(source),
            json!(source_id),
            json!(until),
            json!(unit),
        ],
    )?;
    Ok(())
}
pub fn group_units(c: &Connection, uid: i64, band: i64, session: &str) -> Result<Vec<Value>> {
    let basic = super::lexicon::sql("basic_unit").replace("=binding.learning_unit_id", "=u.id");
    let query="SELECT DISTINCT u.id FROM learning_units u JOIN words w ON w.id=u.word_id JOIN assessment_items i ON i.word=lower(trim(w.word)) AND i.kind='recognition' AND i.band=? WHERE u.active=1 AND NOT EXISTS(SELECT 1 FROM sense_srs_state scheduled WHERE scheduled.user_id=__USER__ AND scheduled.learning_unit_id=u.id AND scheduled.retired_at IS NULL) AND (w.owner_id IS NULL OR w.owner_id=?) AND trim(u.display_definition_cn)=json_extract(i.payload,'$.meaning') AND NOT EXISTS(SELECT 1 FROM readiness_overrides o WHERE o.user_id=? AND o.learning_unit_id=u.id) AND NOT EXISTS(SELECT 1 FROM review_history bad WHERE bad.user_id=? AND bad.learning_unit_id=u.id AND bad.is_first_attempt=1 AND bad.is_correct=0 AND NOT EXISTS(SELECT 1 FROM review_history good WHERE good.user_id=bad.user_id AND good.learning_unit_id=u.id AND good.is_independent=1 AND good.review_time>bad.review_time)) AND NOT EXISTS(SELECT 1 FROM assessment_responses ar JOIN assessment_items ai ON ai.id=ar.item_id WHERE ar.session_id=? AND ar.correct=0 AND ai.word=lower(trim(w.word)) AND json_extract(ai.payload,'$.meaning')=u.display_definition_cn) AND NOT EXISTS(SELECT 1 FROM user_muted_words muted WHERE muted.user_id=? AND muted.word=lower(trim(w.word))) AND NOT EXISTS(SELECT 1 FROM assessment_responses bad JOIN assessment_sessions bs ON bs.id=bad.session_id JOIN assessment_items bi ON bi.id=bad.item_id WHERE bs.user_id=__USER__ AND bad.correct=0 AND bi.word=lower(trim(w.word)) AND json_extract(bi.payload,'$.meaning')=u.display_definition_cn AND NOT EXISTS(SELECT 1 FROM review_history good WHERE good.user_id=bs.user_id AND good.learning_unit_id=u.id AND good.is_independent=1 AND good.review_time>bad.answered_at) AND NOT EXISTS(SELECT 1 FROM assessment_responses good JOIN assessment_sessions gs ON gs.id=good.session_id JOIN assessment_items gi ON gi.id=good.item_id WHERE gs.user_id=bs.user_id AND good.correct=1 AND gi.kind='recall' AND gi.word=bi.word AND json_extract(gi.payload,'$.meaning')=json_extract(bi.payload,'$.meaning') AND good.answered_at>bad.answered_at))".replace("__USER__",&uid.to_string()).replace("SELECT DISTINCT u.id FROM",&format!("SELECT DISTINCT u.id,({basic}) AS basic FROM"));
    rows(
        c,
        &query,
        &[
            json!(band),
            json!(uid),
            json!(uid),
            json!(uid),
            json!(session),
            json!(uid),
        ],
    )
}
pub fn apply_group(
    c: &Connection,
    config: &Config,
    uid: i64,
    band: i64,
    session: &str,
) -> Result<usize> {
    let units = group_units(c, uid, band, session)?;
    for u in &units {
        defer(
            c,
            config,
            uid,
            n(u, "id"),
            "scope_confirmation",
            session,
            7 + n(u, "id") % 8,
        )?;
    }
    Ok(units.len())
}
pub fn retire(c: &Connection, config: &Config, uid: i64) -> Result<()> {
    exec(
        c,
        "UPDATE study_attempts SET completed_at=? WHERE user_id=? AND completed_at IS NULL AND learning_unit_id IN (SELECT id FROM temp.policy_excluded)",
        &[json!(config.stamp()), json!(uid)],
    )?;
    Ok(())
}
pub fn summary(c: &Connection) -> Result<Value> {
    let count = scalar(c, "SELECT COUNT(*) FROM temp.policy_excluded", &[])?;
    Ok(json!({"deferred_units":count,"policy_version":"scope-readiness-v3"}))
}
pub fn handle(c: &Connection, config: &Config, r: &Request, uid: i64) -> Result<Value> {
    if !installed(c) {
        return Err(fault(503, "请先升级学习策略"));
    }
    if r.method == "GET" {
        return Ok(
            json!({"summary":summary(c)?,"units":rows(c,"SELECT u.id,w.word,u.display_definition_cn AS meaning,d.source,d.valid_until FROM temp.policy_excluded e JOIN learning_units u ON u.id=e.id JOIN words w ON w.id=u.word_id JOIN readiness_details d ON d.learning_unit_id=u.id AND d.user_id=? ORDER BY w.word,u.id LIMIT 1000",&[json!(uid)])?}),
        );
    }
    let unit = integer(&r.body, "learning_unit_id")?;
    let own = scalar(
        c,
        "SELECT EXISTS(SELECT 1 FROM user_unit_readiness WHERE user_id=? AND learning_unit_id=?)",
        &[json!(uid), json!(unit)],
    )?;
    if own != json!(1) {
        return Err(fault(404, "暂缓内容不存在"));
    }
    exec(
        c,
        "INSERT OR IGNORE INTO readiness_overrides VALUES(?,?,?)",
        &[json!(uid), json!(unit), json!(config.stamp())],
    )?;
    exec(
        c,
        "UPDATE user_unit_readiness SET state='reinforce' WHERE user_id=? AND learning_unit_id=?",
        &[json!(uid), json!(unit)],
    )?;
    prepare(c, config, uid)?;
    handle(
        c,
        config,
        &Request {
            method: "GET".into(),
            ..r.clone()
        },
        uid,
    )
}

pub fn assessment_failure(c: &Connection, uid: i64, session: &str, sequence: i64) -> Result<()> {
    exec(
        c,
        "UPDATE user_unit_readiness SET state='reinforce' WHERE user_id=? AND learning_unit_id IN(SELECT u.id FROM assessment_responses ar JOIN assessment_items i ON i.id=ar.item_id JOIN words w ON lower(trim(w.word))=i.word JOIN learning_units u ON u.word_id=w.id AND u.display_definition_cn=json_extract(i.payload,'$.meaning') WHERE ar.session_id=? AND ar.sequence=? AND ar.correct=0)",
        &[json!(uid), json!(session), json!(sequence)],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_exclusions_do_not_scan_the_whole_catalogue() {
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        let c = Connection::open_in_memory().unwrap();
        let schema: Vec<String> =
            serde_json::from_str(include_str!("../../../../fixtures/schema-v22.json")).unwrap();
        for statement in schema {
            c.execute_batch(&statement).unwrap();
        }
        // Only the catalogue cardinality matters here; there are no eligible units.
        c.pragma_update(None, "foreign_keys", false).unwrap();
        c.execute_batch("WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<20000) INSERT INTO learning_units(id,lexical_unit_id,word_id,unit_key,representative_sense_id,display_definition_cn,verification) SELECT n,1,1,'unit-'||n,1,'test','legacy' FROM seq").unwrap();
        let operations = Arc::new(AtomicUsize::new(0));
        let counter = operations.clone();
        c.progress_handler(
            1000,
            Some(move || counter.fetch_add(1, Ordering::Relaxed) > 20),
        );
        assert!(
            prepare(&c, &Config::from_env(None), 1).is_ok(),
            "Empty eligibility must not traverse 20,000 units"
        );
        assert!(operations.load(Ordering::Relaxed) < 20);
    }
}
