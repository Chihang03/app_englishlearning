//! New-content ranking and bounded workload updates; never rewrites FSRS.
use super::lexicon::scalar;
use super::*;
pub fn adaptive_profile(c: &Connection, uid: i64) -> Result<Option<Value>> {
    if !crate::assessment_install::installed(c) {
        return Ok(None);
    }
    one(
        c,
        "SELECT * FROM user_proficiency_profiles WHERE user_id=? AND mode='adaptive'",
        &[json!(uid)],
    )
}
pub fn new_unit(c: &Connection, uid: i64) -> Result<Option<Value>> {
    let Some(p) = adaptive_profile(c, uid)? else {
        return Ok(None);
    };
    let count = scalar(
        c,
        "SELECT COUNT(*) FROM proficiency_events WHERE user_id=? AND kind='first_answer'",
        &[json!(uid)],
    )?
    .as_i64()
    .unwrap_or(0);
    let offset = match count % 10 {
        7 | 8 => -1.0,
        9 => 1.0,
        _ => 0.0,
    };
    let mean = p["mean"].as_f64().unwrap_or(0.0);
    // Frequency is an unverified prior: usable for ranking, never for deferral.
    let difficulty = "COALESCE(d.difficulty,CASE WHEN f.zipf_cent IS NULL THEN 0.0 ELSE max(-2.5,min(2.5,(470.0-f.zipf_cent)/65.0)) END)";
    let source=include_str!("new_unit.sql").split("ORDER BY").next().unwrap()
        .replace("SELECT u.id AS learning_unit_id", &format!("SELECT u.id AS learning_unit_id,{difficulty} AS difficulty,COALESCE(d.verified,0) AS verified,d.calibration_version"))
        .replace("LEFT JOIN word_frequencies f ON f.headword=w.normalized", "LEFT JOIN word_frequencies f ON f.headword=w.normalized LEFT JOIN learning_unit_difficulty d ON d.learning_unit_id=u.id AND d.retired=0");
    let query = format!(
        "{source} ORDER BY abs(({difficulty})-:target),u.word_id IN (SELECT word_id FROM touched),f.zipf_cent IS NULL,f.zipf_cent DESC,f.tie_break,original.position,u.id LIMIT 512"
    );
    let mut statement = c.prepare_cached(&query)?;
    let mapped=statement.query_map(rusqlite::named_params!{":user":uid,":skip":false,":target":mean+offset},|r|Ok(json!({"learning_unit_id":r.get::<_,i64>(0)?,"difficulty":r.get::<_,f64>(1)?,"verified":r.get::<_,i64>(2)?,"calibration_version":r.get::<_,Option<String>>(3)?})))?;
    for candidate in mapped {
        let candidate = candidate?;
        if !deferred(c, &p, &candidate)? {
            return Ok(Some(candidate));
        }
    }
    // All candidates temporarily deferred: use the original pool as fallback.
    use rusqlite::OptionalExtension;
    Ok(c.prepare_cached(include_str!("new_unit.sql"))?
        .query_row(rusqlite::named_params! {":user":uid,":skip":false}, |r| {
            Ok(json!({"learning_unit_id":r.get::<_,i64>(0)?}))
        })
        .optional()?)
}
fn deferred(c: &Connection, p: &Value, item: &Value) -> Result<bool> {
    if !b(item, "verified") {
        return Ok(false);
    }
    let gate = scalar(
        c,
        "SELECT EXISTS(SELECT 1 FROM assessment_calibrations WHERE active=1 AND version=?)",
        &[item["calibration_version"].clone()],
    )? == json!(1);
    let lower = p["mean"].as_f64().unwrap() - 1.645 * p["sd"].as_f64().unwrap();
    let known =
        (english_core::assessment::probability(lower, item["difficulty"].as_f64().unwrap()) - 0.25)
            / 0.75;
    Ok(gate && known >= 0.95)
}
pub fn observe(c: &Connection, config: &Config, uid: i64, aid: &str) -> Result<()> {
    if adaptive_profile(c, uid)?.is_none() {
        return Ok(());
    }
    let row = one(
        c,
        "SELECT h.*,w.word,a.hint_used,a.pronunciation_used AS pronunciation,a.answer_exposed FROM review_history h JOIN words w ON w.id=h.word_id JOIN study_attempts a ON a.id=h.attempt_id WHERE h.user_id=? AND h.attempt_id=? ORDER BY h.id LIMIT 1",
        &[json!(uid), json!(aid)],
    )?;
    let Some(row) = row else {
        return Ok(());
    };
    if !b(&row, "is_first_attempt")
        || b(&row, "hint_used")
        || b(&row, "pronunciation")
        || b(&row, "answer_exposed")
    {
        return Ok(());
    }
    let old = scalar(
        c,
        "SELECT EXISTS(SELECT 1 FROM review_history WHERE user_id=? AND learning_unit_id=? AND id<?)",
        &[
            json!(uid),
            row["learning_unit_id"].clone(),
            row["id"].clone(),
        ],
    )?;
    if old == json!(1) {
        return Ok(());
    }
    let fresh=exec(c,"INSERT OR IGNORE INTO proficiency_events(user_id,event_key,kind,word,payload,created_at) VALUES(?,?,'first_answer',?,?,?)",&[json!(uid),json!(format!("first:{}:{}",uid,row["learning_unit_id"])),row["word"].clone(),json!(json!({"correct":b(&row,"is_correct"),"unit":row["learning_unit_id"],"review_id":row["id"]}).to_string()),json!(config.stamp())])?;
    if fresh == 0 {
        return Ok(());
    }
    let events = rows(
        c,
        "SELECT id,word,payload,substr(created_at,1,10) AS day FROM proficiency_events WHERE user_id=? AND kind='first_answer' ORDER BY id DESC LIMIT 50",
        &[json!(uid)],
    )?;
    let unique: std::collections::BTreeSet<_> = events.iter().map(|e| s(e, "word")).collect();
    let days: std::collections::BTreeSet<_> = events.iter().map(|e| s(e, "day")).collect();
    if events.len() < 50 || unique.len() < 30 || days.len() < 3 {
        return Ok(());
    }
    let last = scalar(
        c,
        "SELECT COALESCE(MAX(id),0) FROM proficiency_events WHERE user_id=? AND kind='adjustment'",
        &[json!(uid)],
    )?
    .as_i64()
    .unwrap_or(0);
    if events[49]["id"].as_i64().unwrap_or(0) <= last {
        return Ok(());
    }
    let successes = events
        .iter()
        .filter(|e| serde_json::from_str::<Value>(s(e, "payload")).is_ok_and(|p| b(&p, "correct")))
        .count();
    // Learning samples are selected and exposed: this updates workload within
    // a narrow range, not the formal ability anchor or confidence precision.
    let step = if successes >= 43 {
        0.15
    } else if successes <= 25 {
        -0.15
    } else {
        0.0
    };
    exec(
        c,
        "UPDATE user_proficiency_profiles SET mean=max(initial_mean-0.75,min(initial_mean+0.75,mean+?)),sd=min(2.0,sd+0.02),revision=revision+1,updated_at=? WHERE user_id=?",
        &[json!(step), json!(config.stamp()), json!(uid)],
    )?;
    exec(c,"INSERT INTO proficiency_events(user_id,event_key,kind,payload,created_at) VALUES(?,?,'adjustment',?,?)",&[json!(uid),json!(format!("daily:{}:{}",uid,events[0]["id"])),json!(json!({"step":step,"sample":events.len(),"correct":successes,"method":"bounded-workload-v1"}).to_string()),json!(config.stamp())])?;
    Ok(())
}
