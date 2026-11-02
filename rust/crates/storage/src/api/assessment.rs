//! Account-owned CAT sessions. Scores never touch SRS or review history.
use super::lexicon::scalar;
use super::*;
use english_core::assessment::{self as model, Evidence};
use rand::seq::SliceRandom;
use std::collections::BTreeSet;

// Session versioning keeps in-progress and completed v1 tests unchanged.
const RECOGNITION_MODEL: &str = "vocab-recognition-v2";
fn recognition_only(sess: &Value) -> bool {
    s(sess, "model_version") == RECOGNITION_MODEL
}

fn decode(text: &str) -> Result<Value> {
    serde_json::from_str(text).map_err(|_| fault(500, "Invalid assessment data"))
}
fn session(c: &Connection, uid: i64, id: &str) -> Result<Value> {
    one(
        c,
        "SELECT * FROM assessment_sessions WHERE user_id=? AND id=?",
        &[json!(uid), json!(id)],
    )?
    .ok_or_else(|| fault(404, "测验不存在"))
}
fn evidence(c: &Connection, id: &str) -> Result<Vec<Evidence>> {
    Ok(rows(c,"SELECT i.difficulty,r.correct FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND r.answered_at IS NOT NULL AND i.kind='recognition' ORDER BY r.sequence",&[json!(id)])?.iter().map(|r|Evidence{difficulty:r["difficulty"].as_f64().unwrap(),correct:b(r,"correct")}).collect())
}
fn result(c: &Connection, id: &str) -> Result<Value> {
    let sess = one(
        c,
        "SELECT * FROM assessment_sessions WHERE id=?",
        &[json!(id)],
    )?
    .ok_or_else(|| fault(404, "测验不存在"))?;
    let only = recognition_only(&sess);
    let es = evidence(c, id)?;
    let estimate = model::estimate(&es);
    let confirmation = one(
        c,
        "SELECT target_band FROM assessment_sessions WHERE id=? AND kind='confirmation'",
        &[json!(id)],
    )?;
    let bands = rows(
        c,
        "SELECT i.band,COUNT(*) AS answered,SUM(r.correct) AS correct FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND r.answered_at IS NOT NULL AND i.kind='recognition' GROUP BY i.band ORDER BY i.band",
        &[json!(id)],
    )?;
    let recall=one(c,"SELECT COUNT(*) AS answered,COALESCE(SUM(r.correct),0) AS correct FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND r.answered_at IS NOT NULL AND i.kind='recall'",&[json!(id)])?.unwrap();
    let label = |v: f64| model::LABELS[((v + 2.5).round() as isize).clamp(0, 5) as usize];
    if let Some(scope) = confirmation {
        let correct = es.iter().filter(|e| e.correct).count();
        let sufficient = if only {
            es.len() == 12
        } else {
            es.len() == 8 && n(&recall, "answered") == 4
        };
        let passed = sufficient
            && if only {
                correct >= 11
            } else {
                correct >= 7 && n(&recall, "correct") == 4
            };
        return Ok(
            json!({"confirmation_band":scope["target_band"],"label":"分层确认","range":[],"bands":bands,"recall":recall,"sufficient":sufficient,"passed":passed,"calibrated":false,"boundary":false,"model_version":sess["model_version"]}),
        );
    }
    let sufficient = if only {
        es.len() >= 32 && bands.len() == 6 && bands.iter().all(|b| n(b, "answered") >= 4)
    } else {
        es.len() >= 24 && bands.len() == 6 && n(&recall, "answered") >= 4
    };
    Ok(
        json!({"estimate":estimate,"label":model::LABELS[estimate.stage],"range":[label(estimate.low),label(estimate.high)],"bands":bands,"recall":recall,"model_version":sess["model_version"],"calibrated":false,"sufficient":sufficient,"boundary":es.iter().all(|e|e.correct)||es.iter().all(|e|!e.correct)}),
    )
}
fn current(c: &Connection, id: &str) -> Result<Option<Value>> {
    one(
        c,
        "SELECT r.*,i.payload FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND r.answered_at IS NULL ORDER BY r.sequence LIMIT 1",
        &[json!(id)],
    )
}
fn public_state(c: &Connection, sess: &Value) -> Result<Value> {
    let id = s(sess, "id");
    let count = scalar(
        c,
        "SELECT COUNT(*) FROM assessment_responses WHERE session_id=? AND answered_at IS NOT NULL",
        &[json!(id)],
    )?;
    let question = if let Some(row) = current(c, id)?.filter(|_| s(sess, "state") == "active") {
        let item = decode(s(&row, "payload"))?;
        let order = decode(s(&row, "option_order"))?;
        let options = order
            .as_array()
            .unwrap()
            .iter()
            .map(|i| item["options"][i.as_u64().unwrap() as usize].clone())
            .collect::<Vec<_>>();
        json!({"sequence":row["sequence"],"kind":item["kind"],"prompt":item["prompt"],"options":options})
    } else {
        Value::Null
    };
    let res = if sess["result_json"].is_string() {
        decode(s(sess, "result_json"))?
    } else {
        Value::Null
    };
    let potential = if s(sess, "kind") == "confirmation" && res["passed"] == true {
        {
            let skip = scalar(
                c,
                "SELECT EXISTS(SELECT 1 FROM settings WHERE user_id=? AND key='skip_basic_600' AND value='true')",
                &[sess["user_id"].clone()],
            )? == json!(1);
            super::readiness::group_units(c, n(sess, "user_id"), n(sess, "target_band"), id)?
                .iter()
                .filter(|u| !skip || !b(u, "basic"))
                .count()
        }
    } else {
        0
    };
    Ok(
        json!({"preview":{"eligible_scope_units":potential},"id":id,"state":sess["state"],"answered":count,"maximum":if s(sess,"kind")=="confirmation" {12} else {40},"question":question,"result":res,"applied":!sess["applied_at"].is_null(),"kind":sess["kind"],"target_band":sess["target_band"],"recognition_only":recognition_only(sess),"readiness":super::readiness::summary(c)?}),
    )
}
fn issue(c: &Connection, config: &Config, uid: i64, id: &str) -> Result<()> {
    if current(c, id)?.is_some() {
        return Ok(());
    }
    let es = evidence(c, id)?;
    let est = model::estimate(&es);
    let sess = session(c, uid, id)?;
    let only = recognition_only(&sess);
    let minimum_per_band = if only { 4 } else { 2 };
    let used = rows(
        c,
        "SELECT i.family,i.kind,i.band FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? ORDER BY r.sequence",
        &[json!(id)],
    )?;
    let mut bands = [0usize; 6];
    let mut recall = 0;
    for r in &used {
        if s(r, "kind") == "recognition" {
            bands[n(r, "band") as usize] += 1;
        } else {
            recall += 1;
        }
    }
    let scope = one(
        c,
        "SELECT target_band FROM assessment_sessions WHERE id=? AND kind='confirmation'",
        &[json!(id)],
    )?;
    let done = if scope.is_some() {
        es.len() >= if only { 12 } else { 8 }
    } else if only {
        es.len() >= 40
            || (es.len() >= 32 && bands.iter().all(|n| *n >= minimum_per_band) && est.sd <= 0.6)
    } else {
        es.len() >= 36 || (es.len() >= 24 && bands.iter().all(|n| *n >= 2) && est.sd <= 0.6)
    };
    if done && (only || recall >= 4) {
        let res = result(c, id)?;
        exec(
            c,
            "UPDATE assessment_sessions SET state='completed',completed_at=?,result_json=? WHERE id=?",
            &[json!(config.stamp()), json!(res.to_string()), json!(id)],
        )?;
        return Ok(());
    }
    let kind = if done { "recall" } else { "recognition" };
    let seen: BTreeSet<_> = used.iter().map(|r| s(r, "family").to_owned()).collect();
    // Recent exposures and explicit mutes are not positive ability evidence.
    let candidates = rows(
        c,
        "SELECT i.* FROM assessment_items i WHERE i.active=1 AND i.kind=? AND i.version=(SELECT bank_version FROM assessment_sessions WHERE id=?) AND NOT EXISTS(SELECT 1 FROM user_muted_words m WHERE m.user_id=? AND m.word=i.word) AND NOT EXISTS(SELECT 1 FROM assessment_responses r JOIN assessment_sessions ss ON ss.id=r.session_id JOIN assessment_items old ON old.id=r.item_id WHERE ss.user_id=? AND ss.id!=? AND old.family=i.family AND julianday(r.issued_at)>julianday(?)-30) AND NOT EXISTS(SELECT 1 FROM review_history h JOIN words w ON w.id=h.word_id WHERE h.user_id=? AND lower(trim(w.word))=i.word AND julianday(h.review_time)>julianday(?)-7)",
        &[
            json!(kind),
            json!(id),
            json!(uid),
            json!(uid),
            json!(id),
            json!(config.stamp()),
            json!(uid),
            json!(config.stamp()),
        ],
    )?;
    let route = [1, 3, 0, 4, 2, 5, 1, 3];
    let target = if es.len() < route.len() {
        Some(route[es.len()])
    } else if !done {
        bands.iter().position(|n| *n < minimum_per_band)
    } else {
        None
    };
    let mut eligible: Vec<_> = candidates
        .iter()
        .filter(|r| scope.as_ref().is_none_or(|b| r["band"] == b["target_band"]))
        .filter(|r| !seen.contains(s(r, "family")))
        .collect();
    eligible.shuffle(&mut rand::thread_rng());
    let score = |r: &&Value| {
        let d = r["difficulty"].as_f64().unwrap();
        if let Some(band) = target {
            100.0 - 10.0 * (n(r, "band") - band as i64).abs() as f64
        } else if kind == "recall" {
            -(d - est.mean).abs()
        } else {
            model::information(est.mean, d)
        }
    };
    let Some(chosen) = eligible.iter().max_by(|a, b| score(a).total_cmp(&score(b))) else {
        // No fabricated score when exclusions leave an undersized pool.
        let mut res = result(c, id)?;
        res["sufficient"] = json!(false);
        res["pool_exhausted"] = json!(true);
        exec(
            c,
            "UPDATE assessment_sessions SET state='completed',completed_at=?,result_json=? WHERE id=?",
            &[json!(config.stamp()), json!(res.to_string()), json!(id)],
        )?;
        return Ok(());
    };
    let mut order: Vec<usize> = if kind == "recognition" {
        (0..4).collect()
    } else {
        vec![]
    };
    order.shuffle(&mut rand::thread_rng());
    exec(
        c,
        "INSERT INTO assessment_responses(session_id,sequence,item_id,issued_at,option_order) VALUES(?,?,?,?,?)",
        &[
            json!(id),
            json!(used.len() + 1),
            chosen["id"].clone(),
            json!(config.stamp()),
            json!(json!(order).to_string()),
        ],
    )?;
    Ok(())
}
pub fn profile(c: &Connection, uid: i64) -> Result<Value> {
    let p = one(
        c,
        "SELECT * FROM user_proficiency_profiles WHERE user_id=?",
        &[json!(uid)],
    )?;
    let last = one(
        c,
        "SELECT id,state FROM assessment_sessions WHERE user_id=? ORDER BY rowid DESC LIMIT 1",
        &[json!(uid)],
    )?;
    let gate = scalar(
        c,
        "SELECT EXISTS(SELECT 1 FROM assessment_calibrations WHERE active=1)",
        &[],
    )? == json!(1);
    Ok(
        json!({"enabled":true,"mode":p.as_ref().map_or("frequency",|p|s(p,"mode")),"profile":p,"session":last,"labels":model::LABELS,"automatic_deferral":super::readiness::installed(c),"calibrated_parameters_available":gate,"readiness":super::readiness::summary(c)?}),
    )
}
pub fn handle(c: &Connection, config: &Config, r: &Request, user: &Value) -> Result<Value> {
    let uid = n(user, "id");
    if !crate::assessment_install::installed(c) {
        return Err(fault(503, "词汇测验尚未安装"));
    }
    if r.path == "/api/proficiency" && r.method == "GET" {
        return profile(c, uid);
    }
    if r.path == "/api/proficiency/mode" && r.method == "PATCH" {
        let mode = string(&r.body, "mode", 1, 16)?;
        if !["frequency", "adaptive"].contains(&mode) {
            return Err(fault(422, "未知学习方式"));
        }

        if exec(
            c,
            "UPDATE user_proficiency_profiles SET mode=?,revision=revision+1,updated_at=? WHERE user_id=?",
            &[json!(mode), json!(config.stamp()), json!(uid)],
        )? == 0
        {
            return Err(fault(409, "请先完成并应用词汇测验"));
        }
        super::readiness::refresh_personal(c, config, uid)?;
        super::readiness::prepare(c, config, uid)?;
        return profile(c, uid);
    }
    if r.path == "/api/assessments" && r.method == "POST" {
        if let Some(old) = one(
            c,
            "SELECT * FROM assessment_sessions WHERE user_id=? AND state='active'",
            &[json!(uid)],
        )? {
            return public_state(c, &old);
        }
        let version = scalar(
            c,
            "SELECT version FROM assessment_items WHERE active=1 ORDER BY rowid DESC LIMIT 1",
            &[],
        )?;
        if version.is_null() {
            return Err(fault(503, "测验题库为空"));
        }
        let id = accounts::random_token(24);
        exec(
            c,
            "INSERT INTO assessment_sessions(id,user_id,state,bank_version,model_version,created_at) VALUES(?,?,'active',?,?,?)",
            &[
                json!(id),
                json!(uid),
                version,
                json!(RECOGNITION_MODEL),
                json!(config.stamp()),
            ],
        )?;
        issue(c, config, uid, &id)?;
        return public_state(c, &session(c, uid, &id)?);
    }
    if r.path == "/api/proficiency/confirmations" && r.method == "POST" {
        if !super::readiness::installed(c) {
            return Err(fault(503, "请先升级学习策略"));
        }
        let band = integer(&r.body, "band")?;
        if !(0..=5).contains(&band) {
            return Err(fault(422, "未知层级"));
        }
        if one(
            c,
            "SELECT user_id FROM user_proficiency_profiles WHERE user_id=?",
            &[json!(uid)],
        )?
        .is_none()
        {
            return Err(fault(409, "请先应用水平测验"));
        }
        if let Some(active) = one(
            c,
            "SELECT * FROM assessment_sessions WHERE user_id=? AND state='active'",
            &[json!(uid)],
        )? {
            if s(&active, "kind") != "confirmation" || n(&active, "target_band") != band {
                return Err(fault(409, "请先完成或放弃当前测验"));
            }
            return public_state(c, &active);
        }
        let version = scalar(
            c,
            "SELECT version FROM assessment_items WHERE active=1 ORDER BY rowid DESC LIMIT 1",
            &[],
        )?;
        let id = accounts::random_token(24);
        exec(
            c,
            "INSERT INTO assessment_sessions(id,user_id,state,bank_version,model_version,created_at,kind,target_band) VALUES(?,?,'active',?,?,?,'confirmation',?)",
            &[
                json!(id),
                json!(uid),
                version,
                json!(RECOGNITION_MODEL),
                json!(config.stamp()),
                json!(band),
            ],
        )?;
        issue(c, config, uid, &id)?;
        return public_state(c, &session(c, uid, &id)?);
    }
    let parts: Vec<_> = r.path.trim_matches('/').split('/').collect();
    if parts.len() < 3 || parts[1] != "assessments" {
        return Err(fault(404, "Unknown endpoint"));
    }
    let id = parts[2];
    let sess = session(c, uid, id)?;
    if r.method == "GET" && parts.len() == 3 {
        return public_state(c, &sess);
    }
    if r.method != "POST" || parts.len() != 4 {
        return Err(fault(404, "Unknown endpoint"));
    }
    match parts[3] {
        "abandon" => {
            exec(
                c,
                "UPDATE assessment_sessions SET state='abandoned',completed_at=? WHERE id=? AND state='active'",
                &[json!(config.stamp()), json!(id)],
            )?;
        }
        "answers" => {
            let seq = integer(&r.body, "sequence")?;
            let answer = string(&r.body, "answer", 0, 80)?.trim().to_lowercase();
            let row=one(c,"SELECT r.*,i.payload FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND r.sequence=?",&[json!(id),json!(seq)])?.ok_or_else(||fault(409,"题目顺序已变化，请刷新"))?;
            if row["answered_at"].is_string() {
                if s(&row, "answer") != answer {
                    return Err(fault(409, "该题已提交其他答案"));
                }
                return public_state(c, &sess);
            }
            if s(&sess, "state") != "active" {
                return Err(fault(409, "测验已经结束"));
            }
            let item = decode(s(&row, "payload"))?;
            let expected = if s(&item, "kind") == "recognition" {
                let order = decode(s(&row, "option_order"))?;
                let original: usize = s(&item, "answer")
                    .parse()
                    .map_err(|_| fault(500, "Invalid item answer"))?;
                order
                    .as_array()
                    .unwrap()
                    .iter()
                    .position(|v| v.as_u64() == Some(original as u64))
                    .unwrap()
                    .to_string()
            } else {
                s(&item, "answer").to_string()
            };
            if s(&item, "kind") == "recognition"
                && !["0", "1", "2", "3", ""].contains(&answer.as_str())
            {
                return Err(fault(422, "无效选项"));
            }
            exec(
                c,
                "UPDATE assessment_responses SET answered_at=?,answer=?,correct=? WHERE session_id=? AND sequence=?",
                &[
                    json!(config.stamp()),
                    json!(answer),
                    json!(answer == expected),
                    json!(id),
                    json!(seq),
                ],
            )?;
            if answer != expected {
                super::readiness::assessment_failure(c, uid, id, seq)?;
            }
            issue(c, config, uid, id)?;
        }
        "apply" => {
            if s(&sess, "state") != "completed" {
                return Err(fault(409, "请先完成测验"));
            }
            let res = decode(s(&sess, "result_json"))?;
            if !b(&res, "sufficient") {
                return Err(fault(409, "测验证据不足，请稍后重新测验"));
            }
            if !sess["applied_at"].is_null() {
                return public_state(c, &sess);
            }
            if s(&sess, "kind") == "confirmation" {
                let completed = chrono::DateTime::parse_from_rfc3339(s(&sess, "completed_at"))
                    .map_err(|_| fault(409, "确认结果已失效"))?;
                if config.now() - completed.with_timezone(&Utc) > chrono::Duration::days(7) {
                    return Err(fault(409, "确认结果已过期，请重新进行短确认"));
                }
                if !b(&res, "passed") {
                    return Err(fault(409, "该层级仍需练习，暂不排除"));
                }
                exec(
                    c,
                    "UPDATE user_proficiency_profiles SET mode='adaptive',revision=revision+1,updated_at=? WHERE user_id=?",
                    &[json!(config.stamp()), json!(uid)],
                )?;
                super::readiness::apply_group(c, config, uid, n(&sess, "target_band"), id)?;
                exec(
                    c,
                    "UPDATE assessment_sessions SET applied_at=? WHERE id=?",
                    &[json!(config.stamp()), json!(id)],
                )?;
                super::readiness::prepare(c, config, uid)?;
                let mut out = public_state(c, &session(c, uid, id)?)?;
                out["readiness"] = super::readiness::summary(c)?;
                return Ok(out);
            }
            let newer = scalar(
                c,
                "SELECT EXISTS(SELECT 1 FROM assessment_sessions WHERE user_id=? AND applied_at IS NOT NULL AND rowid>(SELECT rowid FROM assessment_sessions WHERE id=?))",
                &[json!(uid), json!(id)],
            )?;
            if newer == json!(1) {
                return Err(fault(409, "已有更新的测验结果"));
            }

            let e = &res["estimate"];
            exec(
                c,
                "INSERT INTO user_proficiency_profiles(user_id,session_id,mode,mean,sd,initial_mean,model_version,updated_at) VALUES(?,?,'adaptive',?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id,mode='adaptive',mean=excluded.mean,sd=excluded.sd,initial_mean=excluded.initial_mean,model_version=excluded.model_version,revision=user_proficiency_profiles.revision+1,updated_at=excluded.updated_at",
                &[
                    json!(uid),
                    json!(id),
                    e["mean"].clone(),
                    e["sd"].clone(),
                    e["mean"].clone(),
                    res["model_version"].clone(),
                    json!(config.stamp()),
                ],
            )?;
            exec(
                c,
                "UPDATE assessment_sessions SET applied_at=? WHERE id=?",
                &[json!(config.stamp()), json!(id)],
            )?;
            exec(
                c,
                "INSERT INTO proficiency_events(user_id,event_key,kind,payload,created_at) VALUES(?,?,'assessment',?,?)",
                &[
                    json!(uid),
                    json!(format!("apply:{id}")),
                    json!(res.to_string()),
                    json!(config.stamp()),
                ],
            )?;
        }
        _ => return Err(fault(404, "Unknown endpoint")),
    }
    super::readiness::refresh_personal(c, config, uid)?;
    super::readiness::prepare(c, config, uid)?;
    public_state(c, &session(c, uid, id)?)
}
