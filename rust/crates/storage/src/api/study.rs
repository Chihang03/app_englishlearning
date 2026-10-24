use super::lexicon::{scalar, sql};
use super::*;
use chrono::{Datelike, Duration, NaiveDate, TimeZone};
use english_core::fsrs_compat::{Card, retrievability, review};
use std::collections::{BTreeMap, BTreeSet};

fn instant(value: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .map(|v| v.with_timezone(&Utc))
        .map_err(|_| fault(500, "Invalid stored timestamp"))
}
fn stamp(moment: DateTime<Utc>) -> String {
    moment.to_rfc3339_opts(SecondsFormat::Secs, false)
}
pub(super) fn tz(user: &Value, config: &Config) -> chrono_tz::Tz {
    s(user, "timezone")
        .parse()
        .or_else(|_| config.default_timezone.parse())
        .unwrap_or(chrono_tz::UTC)
}
pub(crate) fn bounds(day: NaiveDate, zone: chrono_tz::Tz) -> Result<(String, String)> {
    let at = |day: NaiveDate| {
        zone.from_local_datetime(&day.and_hms_opt(0, 0, 0).unwrap())
            .earliest()
            .ok_or_else(|| fault(500, "Invalid local day"))
    };
    Ok((
        stamp(at(day)?.with_timezone(&Utc)),
        stamp(at(day + Duration::days(1))?.with_timezone(&Utc)),
    ))
}
fn round2(value: f64) -> f64 {
    format!("{value:.2}").parse().unwrap_or(value)
}
fn f(v: &Value, key: &str) -> f64 {
    v[key].as_f64().unwrap_or(0.0)
}
fn unit(c: &Connection, sense: i64) -> Result<i64> {
    scalar(
        c,
        "SELECT learning_unit_id FROM learning_unit_senses WHERE sense_id=?",
        &[json!(sense)],
    )?
    .as_i64()
    .ok_or_else(|| fault(404, "Unknown dictionary sense"))
}
fn anchor(c: &Connection, unit: i64) -> Result<i64> {
    scalar(
        c,
        "SELECT representative_sense_id FROM learning_units WHERE id=?",
        &[json!(unit)],
    )?
    .as_i64()
    .ok_or_else(|| fault(404, "Unknown learning unit"))
}
fn profile(c: &Connection, user: i64) -> Result<Value> {
    Ok(one(
        c,
        "SELECT * FROM memory_profiles WHERE user_id=?",
        &[json!(user)],
    )?
    .unwrap_or_else(
        || json!({"forgetting_multiplier":1.0,"evaluated_count":0,"evaluated_at":null,"adopted":0}),
    ))
}
fn memory_summary(c: &Connection, user: i64) -> Result<Value> {
    let p = profile(c, user)?;
    let count = scalar(
        c,
        "SELECT COUNT(*) FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL",
        &[json!(user)],
    )?;
    Ok(
        json!({"model":english_core::MODEL_VERSION,"target_retention":0.8,"personalized":f(&p,"forgetting_multiplier")!=1.0,
        "sample_count":count,"minimum_samples":150,"forgetting_multiplier":p["forgetting_multiplier"],"evaluated_at":p["evaluated_at"],
        "training_count":n(&p,"training_count"),"validation_count":n(&p,"validation_count"),"baseline_log_loss":p["baseline_log_loss"],"candidate_log_loss":p["candidate_log_loss"]}),
    )
}
fn loss(samples: &[(f64, bool)], multiplier: f64) -> f64 {
    samples
        .iter()
        .map(|(p, correct)| {
            let p = p.powf(multiplier).clamp(1e-6, 1.0 - 1e-6);
            -(if *correct { p } else { 1.0 - p }).ln()
        })
        .sum::<f64>()
        / samples.len() as f64
}
fn calibrate(c: &Connection, user: i64, now: &str) -> Result<()> {
    let count=scalar(c,"SELECT COUNT(*) FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL",&[json!(user)])?.as_i64().unwrap_or(0);
    let previous = profile(c, user)?;
    if count < 150 || count - n(&previous, "evaluated_count") < 50 {
        return Ok(());
    }
    let mut records = rows(
        c,
        "SELECT base_recall_probability,is_correct,COALESCE(learning_unit_id,-sense_id) AS recall_unit,review_time FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL ORDER BY id DESC LIMIT 5000",
        &[json!(user)],
    )?;
    records.reverse();
    if records
        .iter()
        .map(|r| n(r, "recall_unit"))
        .collect::<BTreeSet<_>>()
        .len()
        < 20
        || (instant(s(records.last().unwrap(), "review_time"))?
            - instant(s(&records[0], "review_time"))?)
        .num_days()
            < 30
    {
        return Ok(());
    }
    let samples = records
        .iter()
        .map(|r| (f(r, "base_recall_probability"), b(r, "is_correct")))
        .collect::<Vec<_>>();
    let split = (samples.len() as f64 * 0.8) as usize;
    let (training, validation) = samples.split_at(split);
    let mut candidate = 0.5;
    let mut best = f64::INFINITY;
    for i in 0..61 {
        let value = 0.5 + f64::from(i) * 0.025;
        let score = loss(training, value) + 0.01 * value.ln().powi(2);
        if score < best {
            best = score;
            candidate = value;
        }
    }
    let baseline = loss(validation, 1.0);
    let current = loss(validation, f(&previous, "forgetting_multiplier"));
    let proposed = loss(validation, candidate);
    let adopted = proposed + 0.005 < baseline.min(current);
    let selected = if adopted {
        candidate
    } else if baseline + 0.005 < current {
        1.0
    } else {
        f(&previous, "forgetting_multiplier")
    };
    exec(
        c,
        "INSERT INTO memory_profiles(user_id,forgetting_multiplier,evaluated_count,training_count,validation_count,baseline_log_loss,candidate_log_loss,adopted,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET forgetting_multiplier=excluded.forgetting_multiplier,evaluated_count=excluded.evaluated_count,training_count=excluded.training_count,validation_count=excluded.validation_count,baseline_log_loss=excluded.baseline_log_loss,candidate_log_loss=excluded.candidate_log_loss,adopted=excluded.adopted,evaluated_at=excluded.evaluated_at",
        &[
            json!(user),
            json!(selected),
            json!(count),
            json!(training.len()),
            json!(validation.len()),
            json!(baseline),
            json!(proposed),
            json!(adopted),
            json!(now),
        ],
    )?;
    Ok(())
}
#[allow(clippy::too_many_arguments)]
fn advance(
    c: &Connection,
    user: i64,
    unit_id: i64,
    current: Option<&Value>,
    independent: bool,
    practice: bool,
    assisted: bool,
    today: NaiveDate,
    config: &Config,
) -> Result<Value> {
    let now = instant(&config.stamp())?;
    let stored = one(
        c,
        "SELECT * FROM adaptive_memory WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL",
        &[json!(user), json!(unit_id)],
    )?;
    let mut card = if let Some(stored) = &stored {
        serde_json::from_str::<Card>(s(stored, "card_json"))
            .map_err(|_| fault(500, "Invalid FSRS card"))?
    } else {
        let mut card = Card {
            card_id: unit_id,
            state: 1,
            step: Some(0),
            stability: None,
            difficulty: None,
            due: now,
            last_review: None,
        };
        if let Some(current) = current {
            let last = scalar(
                c,
                "SELECT MAX(review_time) FROM review_history WHERE user_id=? AND learning_unit_id=?",
                &[json!(user), json!(unit_id)],
            )?;
            card.state = 2;
            card.step = None;
            card.stability = Some(f(current, "interval_days").clamp(1.0, 365.0));
            card.difficulty = Some(5.0);
            card.last_review = Some(
                last.as_str()
                    .map(instant)
                    .transpose()?
                    .unwrap_or(now)
                    .min(now),
            );
        }
        card
    };
    card.card_id = unit_id;
    let personal = profile(c, user)?;
    let multiplier = f(&personal, "forgetting_multiplier");
    let retention = 0.8_f64.powf(1.0 / multiplier);
    let base = if card.last_review.is_some() {
        Some(retrievability(&card, now).map_err(|_| fault(500, "Invalid FSRS state"))?)
    } else {
        None
    };
    let eligible = stored.is_some()
        && !practice
        && !assisted
        && card
            .last_review
            .is_some_and(|last| (now - last).num_seconds() >= 86400);
    let prediction = if eligible {
        base.map(|p| p.powf(multiplier))
    } else {
        None
    };
    let mut confirmations = stored.as_ref().map_or(0, |v| n(v, "confirmations"));
    let mut first = stored
        .as_ref()
        .map_or(Value::Null, |v| v["first_independent_at"].clone());
    let mut last = stored
        .as_ref()
        .map_or(Value::Null, |v| v["last_independent_at"].clone());
    if !practice {
        card = review(&card, if independent { 3 } else { 1 }, now, retention)
            .map_err(|_| fault(500, "Invalid FSRS calculation"))?;
        if independent {
            if last.is_null() || (now - instant(last.as_str().unwrap())?).num_seconds() >= 86400 {
                confirmations += 1;
                if first.is_null() {
                    first = json!(config.stamp());
                }
                last = json!(config.stamp());
            }
        } else {
            confirmations = 0;
            first = Value::Null;
            last = Value::Null;
        }
    } else if assisted {
        card.stability = Some(card.stability.unwrap_or(1.0).min(3.0));
        card.last_review = Some(now);
        confirmations = 0;
        first = Value::Null;
        last = Value::Null;
    }
    let probe = Card {
        state: 2,
        stability: Some(card.stability.unwrap_or(1.0)),
        last_review: Some(now),
        ..card.clone()
    };
    let (mut low, mut high) = (0.0, 365.0);
    for _ in 0..18 {
        let middle = (low + high) / 2.0;
        let moment =
            now + Duration::microseconds((middle * 86400.0 * 1e6_f64).round_ties_even() as i64);
        if retrievability(&probe, moment).map_err(|_| fault(500, "Invalid recall probability"))?
            > retention
        {
            low = middle;
        } else {
            high = middle;
        }
    }
    let mut interval = (high.round_ties_even() as i64).max(1);
    let audit = retrievability(&probe, now + Duration::days(180))
        .map_err(|_| fault(500, "Invalid recall probability"))?
        .powf(multiplier);
    let mature = independent && audit >= 0.8;
    if mature {
        interval = 180;
    }
    card.due = now + Duration::days(interval);
    let mut encoded = serde_json::to_value(&card).map_err(|_| fault(500, "Serialize card"))?;
    encoded["due"] = json!(card.due.to_rfc3339_opts(SecondsFormat::AutoSi, false));
    encoded["last_review"] = card
        .last_review
        .map(|v| json!(v.to_rfc3339_opts(SecondsFormat::AutoSi, false)))
        .unwrap_or(Value::Null);
    exec(
        c,
        "INSERT INTO adaptive_memory(user_id,sense_id,learning_unit_id,card_json,known_candidate,confirmations,first_independent_at,last_independent_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,sense_id) DO UPDATE SET card_json=excluded.card_json,known_candidate=excluded.known_candidate,confirmations=excluded.confirmations,first_independent_at=excluded.first_independent_at,last_independent_at=excluded.last_independent_at,learning_unit_id=excluded.learning_unit_id,retired_at=NULL",
        &[
            json!(user),
            json!(anchor(c, unit_id)?),
            json!(unit_id),
            json!(encoded.to_string()),
            json!(0),
            json!(confirmations),
            first,
            last,
        ],
    )?;
    Ok(
        json!({"interval_days":interval,"next_review_date":(today+Duration::days(interval)).to_string(),"mature":mature,"known_candidate":false,"confirmations":confirmations,
        "stability_days":round2(card.stability.unwrap_or(0.0)),"difficulty":round2(card.difficulty.unwrap_or(0.0)),"audit_recall_probability":audit,
        "base_recall_probability":if eligible{base}else{None},"predicted_recall_probability":prediction,"model_version":english_core::MODEL_VERSION,"target_retention":0.8,"personalized":multiplier!=1.0}),
    )
}
fn next_state(current: Option<&Value>, correct: bool, had_wrong: bool, today: NaiveDate) -> Value {
    let mut state = json!({"review_count":0,"correct_count":0,"wrong_count":0,"lapse_count":0,"easiness_factor":2.5,"interval_days":0,"next_review_date":today.to_string(),"status":"New"});
    if let Some(current) = current {
        for (key, value) in current.as_object().unwrap() {
            state[key] = value.clone();
        }
    }
    state["review_count"] = json!(n(&state, "review_count") + 1);
    if !correct {
        state["correct_count"] = json!(0);
        state["wrong_count"] = json!(n(&state, "wrong_count") + 1);
        state["lapse_count"] = json!(n(&state, "lapse_count") + 1);
        state["easiness_factor"] = json!(round2(f(&state, "easiness_factor") - 0.2).max(1.3));
        state["interval_days"] = json!(0);
        state["next_review_date"] = json!(today.to_string());
        state["status"] = json!("Learning");
        return state;
    }
    let count = n(&state, "correct_count") + 1;
    state["correct_count"] = json!(count);
    let ease = round2(f(&state, "easiness_factor") + 0.08).min(3.0);
    state["easiness_factor"] = json!(ease);
    let intervals = [1, 3, 7, 15, 30, 90, 180];
    let days = if count <= 7 {
        intervals[(count - 1) as usize]
    } else {
        (n(&state, "interval_days") as f64 * ease)
            .round_ties_even()
            .max((n(&state, "interval_days") + 1) as f64) as i64
    };
    state["interval_days"] = json!(days);
    state["next_review_date"] = json!((today + Duration::days(days)).to_string());
    state["status"] = json!(if count >= 8 && !had_wrong {
        "Mature"
    } else {
        "Reviewing"
    });
    state
}
fn write_state(c: &Connection, user: i64, sense: i64, state: &Value, example: i64) -> Result<()> {
    let uid = unit(c, sense)?;
    let anchor = anchor(c, uid)?;
    exec(
        c,
        "INSERT INTO sense_srs_state(user_id,sense_id,review_count,correct_count,wrong_count,lapse_count,easiness_factor,interval_days,next_review_date,status,last_example_id,learning_unit_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,sense_id) DO UPDATE SET review_count=excluded.review_count,correct_count=excluded.correct_count,wrong_count=excluded.wrong_count,lapse_count=excluded.lapse_count,easiness_factor=excluded.easiness_factor,interval_days=excluded.interval_days,next_review_date=excluded.next_review_date,status=excluded.status,last_example_id=excluded.last_example_id,learning_unit_id=excluded.learning_unit_id,retired_at=NULL",
        &[
            json!(user),
            json!(anchor),
            state["review_count"].clone(),
            state["correct_count"].clone(),
            state["wrong_count"].clone(),
            state["lapse_count"].clone(),
            state["easiness_factor"].clone(),
            state["interval_days"].clone(),
            state["next_review_date"].clone(),
            state["status"].clone(),
            json!(example),
            json!(uid),
        ],
    )?;
    Ok(())
}

fn example(c: &Connection, uid: i64, user: i64) -> Result<Value> {
    let group = one(c, "SELECT * FROM learning_units WHERE id=?", &[json!(uid)])?
        .ok_or_else(|| fault(404, "Unknown learning unit"))?;
    let examples = rows(
        c,
        "SELECT e.* FROM learning_unit_senses us JOIN word_senses s ON s.id=us.sense_id JOIN sense_examples e ON e.sense_id=s.id JOIN words w ON w.id=s.word_id WHERE us.learning_unit_id=? AND s.active=1 AND s.learning_enabled=1 AND e.active=1 ORDER BY lower(trim(e.target_form))!=lower(trim(w.word)),s.position,e.id",
        &[json!(uid)],
    )?;
    if examples.is_empty() {
        return Err(fault(409, "This sense has no usable example"));
    }
    let fixed = scalar(
        c,
        "SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL",
        &[json!(user), json!(uid)],
    )?;
    let mut chosen = examples.iter().find(|e| e["id"] == fixed).cloned();
    if chosen.is_none() {
        let seen = rows(
            c,
            "SELECT example_id FROM (SELECT example_id,created_at AS seen_at,0 AS source,id AS record_id FROM study_attempts WHERE user_id=? AND learning_unit_id=? UNION ALL SELECT example_id,review_time,1,id FROM review_history WHERE user_id=? AND learning_unit_id=?) ORDER BY julianday(seen_at),source,record_id",
            &[json!(user), json!(uid), json!(user), json!(uid)],
        )?;
        chosen = seen
            .iter()
            .find_map(|r| {
                examples
                    .iter()
                    .find(|e| e["id"] == r["example_id"])
                    .cloned()
            })
            .or_else(|| examples.first().cloned());
    }
    let chosen = chosen.unwrap();
    exec(
        c,
        "INSERT INTO user_sense_examples(user_id,sense_id,example_id,learning_unit_id,retired_at) VALUES(?,?,?,?,NULL) ON CONFLICT(user_id,sense_id) DO UPDATE SET example_id=excluded.example_id,learning_unit_id=excluded.learning_unit_id,retired_at=NULL",
        &[
            json!(user),
            group["representative_sense_id"].clone(),
            chosen["id"].clone(),
            json!(uid),
        ],
    )?;
    Ok(chosen)
}
fn start(c: &Connection, user: i64, sense: i64, example: i64, config: &Config) -> Result<Value> {
    let id = accounts::random_token(24);
    let uid = unit(c, sense)?;
    exec(
        c,
        "INSERT INTO study_attempts(id,user_id,sense_id,example_id,created_at,tracking_version,learning_unit_id) VALUES(?,?,?,?,?,1,?)",
        &[
            json!(id),
            json!(user),
            json!(sense),
            json!(example),
            json!(config.stamp()),
            json!(uid),
        ],
    )?;
    if one(
        c,
        "SELECT 1 FROM sense_exposures WHERE user_id=? AND learning_unit_id=? AND exposed_at>=?",
        &[
            json!(user),
            json!(uid),
            json!(stamp(config.now() - Duration::seconds(1200))),
        ],
    )?
    .is_some()
    {
        exec(
            c,
            "UPDATE study_attempts SET answer_exposed=1 WHERE id=?",
            &[json!(id)],
        )?;
    }
    one(c, "SELECT * FROM study_attempts WHERE id=?", &[json!(id)])?
        .ok_or_else(|| fault(500, "Missing study attempt"))
}
fn make_card(
    c: &Connection,
    user: i64,
    sense: &Value,
    attempt: &Value,
    remaining: Value,
) -> Result<Value> {
    let e = one(
        c,
        "SELECT * FROM sense_examples WHERE id=?",
        &[attempt["example_id"].clone()],
    )?
    .ok_or_else(|| fault(409, "This sense has no usable example"))?;
    let started = one(
        c,
        "SELECT 1 FROM srs_state WHERE user_id=? AND word_id=? UNION ALL SELECT 1 FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=? AND s.word_id=? LIMIT 1",
        &[
            json!(user),
            sense["word_id"].clone(),
            json!(user),
            sense["word_id"].clone(),
        ],
    )?;
    let memory = one(
        c,
        "SELECT confirmations FROM adaptive_memory WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL",
        &[json!(user), sense["learning_unit_id"].clone()],
    )?;
    let queued=one(c,"SELECT 1 FROM relearning_queue WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL",&[json!(user),sense["learning_unit_id"].clone()])?.is_some();
    Ok(
        json!({"id":sense["word_id"],"word":sense["word"],"sense_id":sense["id"],"learning_unit_id":sense["learning_unit_id"],"example_id":e["id"],"part_of_speech":sense["part_of_speech"],
        "definition_cn":if s(sense,"display_definition_cn").is_empty(){&sense["definition_cn"]}else{&sense["display_definition_cn"]},"definition_en":sense["display_definition_en"],
        "cloze_sentence":super::forms::cloze(s(&e,"sentence"),s(&e,"target_form")),"example_sentence":e["sentence"],"example_translation_cn":e["translation_cn"],"answer_form":e["target_form"],
        "answer_form_label":super::forms::label(c,s(sense,"word"),s(&e,"target_form"),s(sense,"part_of_speech"),s(&e,"sentence"))?,
        "status":sense["status"],"is_new_word":started.is_none(),"remaining_today":remaining,"attempt_id":attempt["id"],"needs_correction":b(attempt,"hint_used"),"pronunciation_used":b(attempt,"pronunciation_used"),"answer_exposed":b(attempt,"answer_exposed"),"known_candidate":false,"confirmations":memory.as_ref().map_or(0,|v|n(v,"confirmations")),"is_relearning":queued}),
    )
}
fn next(c: &Connection, config: &Config, user: &Value) -> Result<Value> {
    let id = n(user, "id");
    let today = config.now().with_timezone(&tz(user, config)).date_naive();
    let now = config.stamp();
    let args = [json!(id), json!(id), json!(today.to_string()), json!(now)];
    let active = one(c, sql("next_sense_card/1"), &[json!(id), json!(id)])?;
    exec(c, sql("next_sense_card/2"), &[json!(now), json!(id)])?;
    let remaining = scalar(c, sql("next_sense_card/3"), &args)?;
    if let Some(active) = active {
        let source = one(
            c,
            sql("sense_card_content/0"),
            &[
                json!(id),
                active["sense_id"].clone(),
                active["learning_unit_id"].clone(),
            ],
        )?
        .unwrap();
        return Ok(json!({"card":make_card(c,id,&source,&active,remaining)?}));
    }
    let mut selected = one(c, sql("next_sense_card/4"), &args)?;
    let mut pending = None;
    if selected.is_none() {
        pending = one(
            c,
            sql("next_sense_card/5"),
            &[json!(id), json!(now), json!(id)],
        )?;
        selected = super::query_cache::new_unit(c, id)?;
    }
    let Some(selected) = selected else {
        if let Some(ready) = pending.as_ref().and_then(|r| r["ready_at"].as_str()) {
            let delay = (instant(ready)? - config.now()).num_milliseconds() as f64 / 1000.0;
            return Ok(
                json!({"card":null,"retry_after_seconds":delay.ceil().max(1.0) as i64,"message":"所选词库暂无新词或到期复习，可选择其他词库继续学习。"}),
            );
        }
        return Ok(json!({"card":null,"message":"今天没有到期复习或未学习义项。"}));
    };
    let uid = n(&selected, "learning_unit_id");
    let example = example(c, uid, id)?;
    let source = one(
        c,
        sql("sense_card_content/0"),
        &[json!(id), example["sense_id"].clone(), json!(uid)],
    )?
    .unwrap();
    let attempt = start(c, id, n(&source, "id"), n(&example, "id"), config)?;
    Ok(json!({"card":make_card(c,id,&source,&attempt,remaining)?}))
}
fn related(c: &Connection, user: i64, attempt: &str, config: &Config) -> Result<Value> {
    let source=one(c,"SELECT s.word_id,s.id FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id WHERE a.id=? AND a.user_id=?",&[json!(attempt),json!(user)])?.ok_or_else(||fault(409,"题目不匹配，请加载下一题。"))?;
    exec(
        c,
        "INSERT INTO sense_exposures(user_id,sense_id,exposed_at) SELECT ?,id,? FROM word_senses WHERE word_id=? AND id!=? AND active=1 ON CONFLICT(user_id,sense_id) DO UPDATE SET exposed_at=excluded.exposed_at",
        &[
            json!(user),
            json!(config.stamp()),
            source["word_id"].clone(),
            source["id"].clone(),
        ],
    )?;
    exec(
        c,
        "UPDATE study_attempts SET answer_exposed=1 WHERE user_id=? AND completed_at IS NULL AND learning_unit_id IN (SELECT b.learning_unit_id FROM learning_unit_senses b JOIN word_senses s ON s.id=b.sense_id WHERE s.word_id=? AND s.id!=?)",
        &[json!(user), source["word_id"].clone(), source["id"].clone()],
    )?;
    Ok(json!({"recorded":true}))
}
fn attempt_content(c: &Connection, user: i64, attempt: &str) -> Result<Value> {
    one(c,"SELECT a.id AS attempt_id,a.completed_at,a.sense_id,a.example_id,w.id AS word_id,w.word,w.pronunciation,s.lexical_unit_id,s.part_of_speech,s.definition_cn,s.definition_en,e.sentence,e.translation_cn,e.target_form FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id JOIN words w ON w.id=s.word_id JOIN sense_examples e ON e.id=a.example_id AND e.sense_id=s.id WHERE a.id=? AND a.user_id=? AND (w.owner_id IS NULL OR w.owner_id=?)",&[json!(attempt),json!(user),json!(user)])?.ok_or_else(||fault(404,"找不到这道题目，请重新进入学习。"))
}
fn meanings(c: &Connection, user: i64, p: &Value, config: &Config) -> Result<Value> {
    let attempt = string(p, "attempt_id", 1, 100)?;
    related(c, user, attempt, config)?;
    let content = attempt_content(c, user, attempt)?;
    let detailed = !b(p, "content_cached");
    let mut word = content["word"].clone();
    let mut senses = if detailed {
        lexicon::senses(c, n(&content, "word_id"), user)?
    } else {
        lexicon::sense_ids(c, n(&content, "word_id"))?
    };
    let mut entries = lexicon::entries_mode(c, s(&content, "word"), user, detailed)?;
    let mut morphology = lexicon::morphology(c, content["lexical_unit_id"].as_i64(), user)?;
    if let Some(target) = p["target_lexical_unit_id"]
        .as_i64()
        .filter(|t| Some(*t) != content["lexical_unit_id"].as_i64())
    {
        let allowed = ["derived_words", "related_words"]
            .iter()
            .flat_map(|g| morphology[*g].as_array().unwrap())
            .any(|v| n(v, "lexical_unit_id") == target);
        if !allowed {
            return Err(fault(404, "找不到这条词族关系。"));
        }
        let unit=one(c,"SELECT * FROM lexical_units WHERE id=? AND active=1 AND (owner_id IS NULL OR owner_id=?)",&[json!(target),json!(user)])?.ok_or_else(||fault(404,"找不到这条词族关系。"))?;
        let entry = lexicon::entries_mode(c, s(&unit, "headword"), user, detailed)?
            .into_iter()
            .find(|v| n(v, "lexical_unit_id") == target && s(v, "classification") != "INFLECTION")
            .ok_or_else(|| fault(404, "找不到这条词族关系。"))?;
        word = unit["headword"].clone();
        senses = entry["senses"].as_array().unwrap().clone();
        morphology = entry["morphology"].clone();
        entries = vec![entry];
    }
    if content["completed_at"].is_null() {
        exec(
            c,
            "UPDATE study_attempts SET answer_exposed=1 WHERE id=?",
            &[json!(attempt)],
        )?;
    }
    let mut exposed = senses.iter().map(|v| n(v, "id")).collect::<BTreeSet<_>>();
    let mut spellings = BTreeSet::from([word.as_str().unwrap_or("").to_owned()]);
    for metadata in std::iter::once(&morphology).chain(entries.iter().map(|e| &e["morphology"])) {
        for form in metadata["inflected_forms"].as_array().unwrap() {
            spellings.insert(s(form, "spelling").to_owned());
        }
        for group in ["derived_words", "related_words"] {
            for peer in metadata[group].as_array().unwrap() {
                spellings.insert(s(peer, "headword").to_owned());
            }
        }
    }
    for spelling in spellings {
        for sense in lexicon::entry_sense_ids(c, &spelling, user)? {
            exposed.insert(n(&sense, "id"));
        }
    }
    for &sense in &exposed {
        if sense != n(&content, "sense_id") {
            exec(
                c,
                "INSERT INTO sense_exposures(user_id,sense_id,exposed_at) VALUES(?,?,?) ON CONFLICT(user_id,sense_id) DO UPDATE SET exposed_at=excluded.exposed_at",
                &[json!(user), json!(sense), json!(config.stamp())],
            )?;
        }
        exec(
            c,
            "UPDATE study_attempts SET answer_exposed=1 WHERE user_id=? AND sense_id=? AND completed_at IS NULL",
            &[json!(user), json!(sense)],
        )?;
    }
    exposed.insert(n(&content, "sense_id"));
    if b(p, "content_cached") {
        return Ok(json!({"exposed_sense_ids":exposed}));
    }
    Ok(
        json!({"word":word,"senses":senses,"entries":entries,"morphology":morphology,"exposed_sense_ids":exposed}),
    )
}
fn record_review(c: &Connection, config: &Config, user: &Value, p: &Value) -> Result<Value> {
    let id = n(user, "id");
    let today = config.now().with_timezone(&tz(user, config)).date_naive();
    let now = config.stamp();
    let word = integer(p, "word_id")?;
    let (mut sense, mut example) = (p["sense_id"].as_i64(), p["example_id"].as_i64());
    if sense.is_some() != example.is_some() {
        return Err(fault(422, "Submit both sense_id and example_id"));
    }
    if sense.is_none() {
        let pairs = rows(c, sql("record_sense_review/1"), &[json!(word), json!(id)])?;
        if pairs.len() != 1 {
            return Err(fault(422, "Select a specific sense_id and example_id"));
        }
        sense = Some(n(&pairs[0], "sense_id"));
        example = Some(n(&pairs[0], "example_id"));
    }
    let (sense, example) = (sense.unwrap(), example.unwrap());
    let row = one(
        c,
        sql("record_sense_review/2"),
        &[json!(example), json!(sense), json!(word), json!(id)],
    )?
    .ok_or_else(|| fault(404, "Sense/example pair not found"))?;
    if super::forms::occurrences(s(&row, "sentence"), s(&row, "target_form")).is_empty() {
        return Err(fault(404, "Sense/example pair not found"));
    }
    if one(
        c,
        sql("record_sense_review/3"),
        &[json!(id), row["word"].clone()],
    )?
    .is_some()
    {
        return Err(fault(409, "此单词已设为不再学习，请加载下一题。"));
    }
    let uid = unit(c, sense)?;
    let anchor_id = anchor(c, uid)?;
    if one(
        c,
        sql("record_sense_review/4"),
        &[json!(uid), json!(id), json!(id)],
    )?
    .is_none()
    {
        return Err(fault(409, "此单词已跳过，请加载下一题。"));
    }
    if p["learning_unit_id"].as_i64().is_some_and(|v| v != uid) {
        return Err(fault(409, "本轮学习义项不匹配，请加载下一题。"));
    }
    let (day_start, day_end) = bounds(today, tz(user, config))?;
    if one(
        c,
        sql("record_sense_review/5"),
        &[json!(id), json!(uid), json!(day_start), json!(day_end)],
    )?
    .is_some()
    {
        return Err(fault(409, "这个义项今天已独立通过，请加载下一题。"));
    }
    let attempt = if let Some(attempt) = p["attempt_id"].as_str() {
        let row = one(
            c,
            sql("record_sense_review/6"),
            &[json!(attempt), json!(id), json!(sense), json!(example)],
        )?
        .filter(|r| n(r, "learning_unit_id") == uid);
        row.ok_or_else(|| fault(409, "本轮题目已结束或不匹配，请加载下一题。"))?
    } else if let Some(attempt) = one(c, sql("record_sense_review/7"), &[json!(id), json!(uid)])? {
        if n(&attempt, "example_id") != example || n(&attempt, "sense_id") != sense {
            return Err(fault(409, "请先完成当前例句的纠正。"));
        }
        attempt
    } else {
        let pending = one(c, sql("record_sense_review/8"), &[json!(id), json!(uid)])?;
        if pending
            .as_ref()
            .is_some_and(|v| s(v, "ready_at") > now.as_str())
        {
            return Err(fault(409, "待巩固词需要间隔后再独立作答。"));
        }
        let scheduled = one(c, sql("record_sense_review/9"), &[json!(id), json!(uid)])?;
        if pending.is_none()
            && scheduled.as_ref().is_some_and(|r| {
                s(r, "next_review_date") > today.to_string().as_str() || s(r, "status") == "Mature"
            })
        {
            return Err(fault(409, "这个义项尚未到复习时间，请加载下一题。"));
        }
        if scheduled.is_none()
            && one(
                c,
                sql("record_sense_review/10"),
                &[json!(uid), json!(id), json!(id), json!(id), json!(id)],
            )?
            .is_none()
        {
            return Err(fault(409, "此词义不在所选词库的学习范围内。"));
        }
        start(c, id, sense, example, config)?
    };
    let answer = p["user_answer"].as_str().unwrap_or("").trim();
    if answer.chars().count() > 200 {
        return Err(fault(422, "Answer too long"));
    }
    if p["active_response_ms"]
        .as_i64()
        .is_some_and(|n| !(0..=300000).contains(&n))
    {
        return Err(fault(422, "Invalid active_response_ms"));
    }
    let normalized = |v: &str| fold(&v.split_whitespace().collect::<Vec<_>>().join(" "));
    let correct = normalized(answer) == normalized(s(&row, "target_form"));
    let assisted = b(&attempt, "pronunciation_used") || b(&attempt, "answer_exposed");
    let independent = correct && !b(&attempt, "hint_used") && !assisted;
    let current = one(c, sql("record_sense_review/11"), &[json!(id), json!(uid)])?;
    let had_wrong = one(
        c,
        sql("record_sense_review/12"),
        &[
            json!(id),
            json!(uid),
            json!(stamp(config.now() - Duration::days(180))),
        ],
    )?
    .is_some();
    let memory = advance(
        c,
        id,
        uid,
        current.as_ref(),
        independent,
        b(&attempt, "hint_used") || (correct && !independent),
        assisted,
        today,
        config,
    )?;
    exec(
        c,
        sql("record_sense_review/13"),
        &[
            json!(id),
            json!(word),
            json!(sense),
            json!(example),
            json!(now),
            json!(answer),
            json!(correct),
            attempt["id"].clone(),
            json!(independent),
            json!(!b(&attempt, "hint_used") && !assisted),
            json!(b(&attempt, "pronunciation_used")),
            p["active_response_ms"].clone(),
            memory["base_recall_probability"].clone(),
            memory["predicted_recall_probability"].clone(),
            memory["model_version"].clone(),
        ],
    )?;
    let mut state = if correct && !independent {
        let mut state = current
            .clone()
            .unwrap_or_else(|| next_state(None, true, false, today));
        if current.is_none() {
            state["review_count"] = json!(0);
        }
        state["review_count"] = json!(n(&state, "review_count") + 1);
        if assisted {
            state["correct_count"] = json!(0);
            state["interval_days"] = json!(0);
            state["next_review_date"] = json!(today.to_string());
            state["status"] = json!("Learning");
        }
        state
    } else {
        next_state(current.as_ref(), correct, had_wrong, today)
    };
    if independent {
        state["interval_days"] = memory["interval_days"].clone();
        state["next_review_date"] = memory["next_review_date"].clone();
        state["status"] = json!(if b(&memory, "mature") {
            "Mature"
        } else {
            "Reviewing"
        });
    }
    for key in [
        "user_id",
        "sense_id",
        "last_example_id",
        "learning_unit_id",
        "retired_at",
    ] {
        state.as_object_mut().unwrap().remove(key);
    }
    write_state(c, id, sense, &state, example)?;
    if independent {
        exec(c, sql("record_sense_review/14"), &[json!(id), json!(uid)])?;
    } else {
        let order = scalar(c, sql("record_sense_review/15"), &[json!(id)])?;
        exec(
            c,
            sql("record_sense_review/16"),
            &[
                json!(id),
                json!(anchor_id),
                json!(now),
                json!(stamp(config.now() + Duration::seconds(1201))),
                order,
                json!(uid),
            ],
        )?;
    }
    exec(
        c,
        sql(if correct {
            "record_sense_review/17"
        } else {
            "record_sense_review/18"
        }),
        if correct {
            vec![json!(now), attempt["id"].clone()]
        } else {
            vec![attempt["id"].clone()]
        }
        .as_slice(),
    )?;
    calibrate(c, id, &now)?;
    let mut public_memory = memory.clone();
    for key in ["base_recall_probability", "mature"] {
        public_memory.as_object_mut().unwrap().remove(key);
    }
    let other = lexicon::senses(c, word, id)?
        .into_iter()
        .filter(|s| n(s, "id") != sense)
        .collect::<Vec<_>>();
    Ok(
        json!({"is_correct":correct,"is_independent":independent,"is_blank":answer.is_empty(),"outcome":if independent{"independent"}else if correct&&assisted&&!b(&attempt,"hint_used"){"assisted"}else if correct{"corrected"}else{"incorrect"},"memory":public_memory,
        "correct_answer":row["target_form"],"word":row["word"],"learning_unit_id":uid,"sense_id":sense,"example_id":example,"example_sentence":row["sentence"],"definition_cn":row["definition_cn"],"definition_en":row["definition_en"],"srs_state":state,"other_senses":other}),
    )
}

fn metrics(c: &Connection, id: i64, today: NaiveDate, now: &str) -> Result<Value> {
    let mut result = super::query_cache::metrics(c, id, &today.to_string(), now)?;
    result["due_review"] = result["due_senses"].clone();
    result["memory_model"] = memory_summary(c, id)?;
    Ok(result)
}
fn stats(c: &Connection, config: &Config, user: &Value) -> Result<Value> {
    let id = n(user, "id");
    let zone = tz(user, config);
    let today = config.now().with_timezone(&zone).date_naive();
    let (start, end) = bounds(today, zone)?;
    let row = one(c, sql("stats/0"), &[json!(id), json!(start), json!(end)])?.unwrap();
    let total = scalar(c, sql("stats/1"), &[json!(id)])?;
    // Keep dates only, rather than materializing every history row as JSON.
    let mut statement = c.prepare_cached(sql("stats/2"))?;
    let mut history = statement.query(rusqlite::params![
        id,
        stamp(config.now() - Duration::days(400))
    ])?;
    let mut days = BTreeSet::new();
    while let Some(row) = history.next()? {
        let value: String = row.get(0)?;
        if let Ok(moment) = instant(&value) {
            days.insert(moment.with_timezone(&zone).date_naive());
        }
    }
    let mut streak = 0;
    let mut cursor = today;
    while days.contains(&cursor) {
        streak += 1;
        cursor -= Duration::days(1);
    }
    let ratio = |a: &str, b: &str| {
        if n(&row, b) > 0 {
            json!((f(&row, a) / f(&row, b) * 100.0).round_ties_even() as i64)
        } else {
            Value::Null
        }
    };
    let mut result = metrics(c, id, today, &config.stamp())?;
    for (key,value) in json!({"today_learning":row["total"],"total_study_time_ms":total,"today_completed_cards":row["completed_cards"],
        "today_accuracy":if n(&row,"total")==0{json!(0)}else{ratio("correct","total")},"today_success":row["success_words"],"today_success_senses":row["success_senses"],
        "today_independent_accuracy":ratio("first_correct","first_total"),"streak_days":streak}).as_object().unwrap(){result[key]=value.clone();}
    Ok(result)
}
fn calendar(
    c: &Connection,
    config: &Config,
    user: &Value,
    requested: Option<&str>,
) -> Result<Value> {
    let zone = tz(user, config);
    let today = config.now().with_timezone(&zone).date_naive();
    let month = requested
        .map(str::to_owned)
        .unwrap_or_else(|| today.format("%Y-%m").to_string());
    let invalid = || fault(422, "月份格式应为 YYYY-MM。");
    if month.len() != 7
        || !regex::Regex::new(r"^[0-9]{4}-[0-9]{2}$")
            .unwrap()
            .is_match(&month)
    {
        return Err(invalid());
    }
    let first =
        NaiveDate::parse_from_str(&format!("{month}-01"), "%Y-%m-%d").map_err(|_| invalid())?;
    if !(1..=9999).contains(&first.year()) {
        return Err(invalid());
    }
    let after = if first.month() == 12 {
        NaiveDate::from_ymd_opt(first.year() + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(first.year(), first.month() + 1, 1)
    }
    .ok_or_else(invalid)?;
    if after.year() > 9999 {
        return Err(invalid());
    }
    let (start, _) = bounds(first, zone)?;
    let (end, _) = bounds(after, zone)?;
    let mut records = BTreeMap::<NaiveDate, Vec<Value>>::new();
    for row in rows(
        c,
        "SELECT id,review_time,attempt_id,is_correct,is_first_attempt,active_response_ms FROM review_history WHERE user_id=? AND review_time>=? AND review_time<?",
        &[user["id"].clone(), json!(start), json!(end)],
    )? {
        records
            .entry(
                instant(s(&row, "review_time"))?
                    .with_timezone(&zone)
                    .date_naive(),
            )
            .or_default()
            .push(row);
    }
    let mut days = Vec::new();
    let (mut learning_days, mut total_completed) = (0, 0);
    let mut day = first;
    while day < after {
        let items = records.get(&day).cloned().unwrap_or_default();
        let mut completed = BTreeSet::new();
        let (mut time, mut timed, mut first_total, mut first_correct) = (0, 0, 0, 0);
        for item in &items {
            if b(item, "is_correct") {
                completed.insert(
                    item["attempt_id"]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| format!("legacy:{}", n(item, "id"))),
                );
            }
            if !item["active_response_ms"].is_null() {
                timed += 1;
                time += n(item, "active_response_ms");
            }
            if b(item, "is_first_attempt") {
                first_total += 1;
                if b(item, "is_correct") {
                    first_correct += 1;
                }
            }
        }
        if !items.is_empty() {
            learning_days += 1;
        }
        total_completed += completed.len();
        days.push(json!({"date":day.to_string(),"reviews":items.len(),"completed_cards":completed.len(),"study_time_ms":if timed>0||items.is_empty(){json!(time)}else{Value::Null},
            "first_attempt_accuracy":if first_total>0{json!((f64::from(first_correct)/f64::from(first_total)*100.0).round_ties_even() as i64)}else{Value::Null}}));
        day += Duration::days(1);
    }
    Ok(
        json!({"month":month,"today":today.to_string(),"days":days,"summary":{"learning_days":learning_days,"completed_cards":total_completed}}),
    )
}

pub fn handle(c: &Connection, config: &Config, r: &Request, user: &Value) -> Result<Value> {
    let uid = n(user, "id");
    let p = &r.body;
    match (r.method.as_str(), r.path.as_str()) {
        ("GET", "/api/stats") => stats(c, config, user),
        ("GET", "/api/learning-calendar") => {
            calendar(c, config, user, r.query.get("month").map(String::as_str))
        }
        ("GET", "/api/next") => next(c, config, user),
        ("POST", "/api/review") => record_review(c, config, user, p),
        ("POST", "/api/study/hint") => {
            let attempt = string(p, "attempt_id", 1, 100)?;
            let kind = string(p, "kind", 1, 20)?;
            if !["pronunciation", "answer"].contains(&kind) {
                return Err(fault(422, "Invalid hint kind"));
            }
            if one(
                c,
                "SELECT * FROM study_attempts WHERE id=? AND user_id=? AND completed_at IS NULL",
                &[json!(attempt), json!(uid)],
            )?
            .is_none()
            {
                return Err(fault(409, "本轮题目已结束，请加载下一题。"));
            }
            exec(
                c,
                if kind == "pronunciation" {
                    "UPDATE study_attempts SET pronunciation_used=1 WHERE id=?"
                } else {
                    "UPDATE study_attempts SET answer_exposed=1 WHERE id=?"
                },
                &[json!(attempt)],
            )?;
            Ok(json!({"recorded":true}))
        }
        ("POST", "/api/study/related-exposure") => {
            related(c, uid, string(p, "attempt_id", 1, 100)?, config)
        }
        ("POST", "/api/study/meanings") => meanings(c, uid, p, config),
        ("POST", "/api/content-reports") => {
            let attempt = string(p, "attempt_id", 1, 100)?;
            let category = string(p, "category", 1, 30)?;
            if ![
                "definition",
                "sentence",
                "translation",
                "pronunciation",
                "other",
            ]
            .contains(&category)
            {
                return Err(fault(422, "Invalid report category"));
            }
            let details = p["details"].as_str().unwrap_or("");
            if details.chars().count() > 2000 {
                return Err(fault(422, "Report too long"));
            }
            let content = attempt_content(c, uid, attempt)?;
            exec(
                c,
                "INSERT INTO content_reports(user_id,attempt_id,word_id,sense_id,example_id,category,details,content_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,attempt_id,category) DO NOTHING",
                &[
                    json!(uid),
                    json!(attempt),
                    content["word_id"].clone(),
                    content["sense_id"].clone(),
                    content["example_id"].clone(),
                    json!(category),
                    json!(details.trim()),
                    json!(content.to_string()),
                    json!(config.stamp()),
                ],
            )?;
            let id = scalar(
                c,
                "SELECT id FROM content_reports WHERE user_id=? AND attempt_id=? AND category=?",
                &[json!(uid), json!(attempt), json!(category)],
            )?;
            Ok(json!({"id":id,"status":"received"}))
        }
        _ => Err(fault(404, "Not Found")),
    }
}
