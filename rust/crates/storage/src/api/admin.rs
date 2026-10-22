//! Administrator operations and optimistic, atomic report-review exchange.
use super::lexicon::{scalar, sql};
use super::*;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
const FIELDS: [&str; 6] = [
    "definition_cn",
    "definition_en",
    "sentence",
    "translation_cn",
    "target_form",
    "pronunciation",
];
const CONTENT: [&str; 8] = [
    "word",
    "part_of_speech",
    "definition_cn",
    "definition_en",
    "sentence",
    "translation_cn",
    "target_form",
    "pronunciation",
];
fn subset(v: &Value, keys: &[&str]) -> Value {
    Value::Object(keys.iter().map(|k| ((*k).into(), v[*k].clone())).collect())
}
fn parse(v: &Value) -> Result<Value> {
    serde_json::from_str(v.as_str().unwrap_or("")).map_err(|_| fault(500, "Invalid stored content"))
}
fn today(user: &Value, config: &Config) -> Result<(String, String)> {
    let zone = study::tz(user, config);
    study::bounds(config.now().with_timezone(&zone).date_naive(), zone)
}
fn page(r: &Request, key: &str, default: i64, min: i64, max: i64) -> Result<i64> {
    let value = r
        .query
        .get(key)
        .map(|s| s.parse::<i64>())
        .transpose()
        .map_err(|_| fault(422, "Invalid pagination"))?
        .unwrap_or(default);
    if !(min..=max).contains(&value) {
        return Err(fault(422, "Invalid pagination"));
    }
    Ok(value)
}
pub fn detail(c: &Connection, id: i64) -> Result<Value> {
    let mut report = one(c, sql("report_detail_from/0"), &[json!(id)])?
        .ok_or_else(|| fault(404, "反馈不存在"))?;
    let snapshot = parse(&report["content_snapshot"])?;
    report.as_object_mut().unwrap().remove("content_snapshot");
    report["content"] = subset(&snapshot, &CONTENT);
    let current = one(c, sql("report_detail_from/1"), &[json!(id)])?;
    report["editable"] = json!(
        current
            .as_ref()
            .is_some_and(|v| b(v, "sense_active") && b(v, "example_active"))
    );
    report["current_content"] = current.map(|v| subset(&v, &CONTENT)).unwrap_or(Value::Null);
    let mut edits = rows(c, sql("report_detail_from/2"), &[json!(id)])?;
    for e in &mut edits {
        e["before"] = parse(&e["before_json"])?;
        e["after"] = parse(&e["after_json"])?;
        e.as_object_mut().unwrap().remove("before_json");
        e.as_object_mut().unwrap().remove("after_json");
    }
    report["edits"] = json!(edits);
    Ok(report)
}
fn normalized(v: &Value) -> Value {
    let mut result = Map::new();
    for k in FIELDS {
        if let Some(value) = v.get(k) {
            let text = value.as_str().unwrap_or("").trim();
            result.insert(
                k.into(),
                if text.is_empty()
                    && ["definition_en", "translation_cn", "pronunciation"].contains(&k)
                {
                    Value::Null
                } else {
                    json!(text)
                },
            );
        }
    }
    Value::Object(result)
}
fn checked(before: &Value, input: &Value) -> Result<(Value, Value)> {
    let mut changes = normalized(input);
    changes
        .as_object_mut()
        .unwrap()
        .retain(|k, v| before[k] != *v);
    if changes.as_object().unwrap().is_empty() {
        return Err(fault(400, "没有修改内容"));
    }
    let mut after = before.clone();
    for (k, v) in changes.as_object().unwrap() {
        after[k] = v.clone();
    }
    if s(&after, "definition_cn").is_empty() && s(&after, "definition_en").is_empty() {
        return Err(fault(400, "至少保留一项释义"));
    }
    if s(&after, "sentence").is_empty()
        || s(&after, "target_form").is_empty()
        || forms::occurrences(s(&after, "sentence"), s(&after, "target_form")).is_empty()
    {
        return Err(fault(400, "例句必须包含正确的答案词形"));
    }
    Ok((changes, after))
}
fn store_override(
    c: &Connection,
    config: &Config,
    entity: &str,
    id: i64,
    original: Value,
    changes: Value,
    admin: i64,
) -> Result<()> {
    if changes.as_object().is_none_or(|v| v.is_empty()) {
        return Ok(());
    }
    let old = scalar(
        c,
        "SELECT values_json FROM admin_content_overrides WHERE entity=? AND entity_id=?",
        &[json!(entity), json!(id)],
    )?;
    let mut values = if old.is_null() {
        json!({})
    } else {
        parse(&old)?
    };
    for (k, v) in changes.as_object().unwrap() {
        values[k] = v.clone();
    }
    exec(
        c,
        "INSERT INTO admin_content_overrides(entity,entity_id,original_json,values_json,updated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(entity,entity_id) DO UPDATE SET values_json=excluded.values_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        &[
            json!(entity),
            json!(id),
            json!(original.to_string()),
            json!(values.to_string()),
            json!(admin),
            json!(config.stamp()),
        ],
    )?;
    Ok(())
}
fn pair(c: &Connection, id: i64) -> Result<Value> {
    one(
        c,
        "SELECT word_id,sense_id,example_id FROM content_reports WHERE id=?",
        &[json!(id)],
    )?
    .ok_or_else(|| fault(404, "反馈不存在"))
}
pub fn correct(c: &Connection, config: &Config, id: i64, p: &Value, admin: i64) -> Result<Value> {
    let report = detail(c, id)?;
    if !b(&report, "editable") {
        return Err(fault(409, "题目已归档，无法修改"));
    }
    let before = &report["current_content"];
    if *before != p["expected_content"] {
        return Err(fault(409, "内容已被更新，请刷新后再修改"));
    }
    let (changes, after) = checked(before, &p["changes"])?;
    let pair = pair(c, id)?;
    let (wid, sid, eid) = (
        n(&pair, "word_id"),
        n(&pair, "sense_id"),
        n(&pair, "example_id"),
    );
    if !scalar(
        c,
        "SELECT 1 FROM sense_examples WHERE sense_id=? AND sentence=? AND id!=?",
        &[json!(sid), after["sentence"].clone(), json!(eid)],
    )?
    .is_null()
    {
        return Err(fault(409, "此义项已存在相同例句"));
    }
    let sense_keys = ["definition_cn", "definition_en"];
    let example_keys = ["sentence", "translation_cn", "target_form"];
    let has = |keys: &[&str]| keys.iter().any(|k| changes.get(*k).is_some());
    if has(&sense_keys) {
        let updates = Value::Object(
            changes
                .as_object()
                .unwrap()
                .iter()
                .filter(|(k, _)| sense_keys.contains(&k.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        );
        store_override(
            c,
            config,
            "sense",
            sid,
            subset(before, &sense_keys),
            updates,
            admin,
        )?;
        exec(
            c,
            "UPDATE word_senses SET definition_cn=?,definition_en=? WHERE id=?",
            &[
                after["definition_cn"].clone(),
                after["definition_en"].clone(),
                json!(sid),
            ],
        )?;
    }
    if has(&example_keys) {
        store_override(
            c,
            config,
            "example",
            eid,
            subset(before, &example_keys),
            subset(&after, &example_keys),
            admin,
        )?;
        exec(
            c,
            "UPDATE sense_examples SET sentence=?,translation_cn=?,target_form=? WHERE id=?",
            &[
                after["sentence"].clone(),
                after["translation_cn"].clone(),
                after["target_form"].clone(),
                json!(eid),
            ],
        )?;
    }
    let word = one(c, "SELECT * FROM words WHERE id=?", &[json!(wid)])?
        .ok_or_else(|| fault(404, "Unknown word"))?;
    let mut wc = json!({});
    if let Some(v) = changes.get("pronunciation") {
        wc["pronunciation"] = v.clone();
    }
    for k in sense_keys {
        if let Some(v) = changes.get(k) {
            if word[k] == before[k] {
                wc[k] = v.clone();
            }
        }
    }
    store_override(
        c,
        config,
        "word",
        wid,
        subset(&word, &["pronunciation", "definition_cn", "definition_en"]),
        wc.clone(),
        admin,
    )?;
    for (k, v) in wc.as_object().unwrap() {
        exec(
            c,
            &format!("UPDATE words SET {k}=? WHERE id=?"),
            &[v.clone(), json!(wid)],
        )?;
    }
    let stamp = json!(config.stamp());
    if has(&sense_keys) || has(&example_keys) {
        exec(
            c,
            "UPDATE learning_units SET provenance_json=json_set(provenance_json,'$.review_pending',1) WHERE word_id=? AND active=1 AND unit_key LIKE 'semantic:%'",
            &[json!(wid)],
        )?;
        exec(
            c,
            "UPDATE study_attempts SET completed_at=? WHERE completed_at IS NULL AND learning_unit_id IN (SELECT id FROM learning_units WHERE word_id=? AND active=1 AND unit_key LIKE 'semantic:%')",
            &[stamp.clone(), json!(wid)],
        )?;
    }
    if has(&sense_keys) {
        exec(
            c,
            "UPDATE study_attempts SET completed_at=? WHERE sense_id=? AND completed_at IS NULL",
            &[stamp.clone(), json!(sid)],
        )?;
    } else if has(&example_keys) {
        exec(
            c,
            "UPDATE study_attempts SET completed_at=? WHERE example_id=? AND completed_at IS NULL",
            &[stamp.clone(), json!(eid)],
        )?;
    }
    exec(
        c,
        "INSERT INTO admin_content_edits(report_id,admin_id,before_json,after_json,created_at) VALUES(?,?,?,?,?)",
        &[
            json!(id),
            json!(admin),
            json!(before.to_string()),
            json!(after.to_string()),
            stamp.clone(),
        ],
    )?;
    exec(
        c,
        "UPDATE content_reports SET status='resolved',resolution_notes=?,resolved_at=?,resolved_by=? WHERE id=?",
        &[
            json!(s(p, "resolution_notes").trim()),
            stamp,
            json!(admin),
            json!(id),
        ],
    )?;
    detail(c, id)
}
fn sorted(v: &Value) -> Value {
    match v {
        Value::Object(o) => Value::Object(
            o.iter()
                .collect::<std::collections::BTreeMap<_, _>>()
                .into_iter()
                .map(|(k, v)| (k.clone(), sorted(v)))
                .collect(),
        ),
        Value::Array(a) => json!(a.iter().map(sorted).collect::<Vec<_>>()),
        _ => v.clone(),
    }
}
fn digest(v: &Value) -> String {
    format!("{:x}", Sha256::digest(sorted(v).to_string().as_bytes()))
}
fn export(c: &Connection, limit: i64, after: i64) -> Result<Value> {
    let mut records = Vec::new();
    for row in rows(
        c,
        "SELECT id FROM content_reports WHERE status='pending' AND id>? ORDER BY id LIMIT ?",
        &[json!(after), json!(limit)],
    )? {
        let id = n(&row, "id");
        let live = detail(c, id)?;
        let p = pair(c, id)?;
        records.push(json!({"id":id,"category":live["category"],"details":live["details"],"reported_content":live["content"],"expected_content":live["current_content"],"editable":live["editable"],"pair":[p["word_id"],p["sense_id"],p["example_id"]]}));
    }
    let records = json!(records);
    Ok(
        json!({"format_version":1,"task":"content_reports","batch":digest(&records),"records":records}),
    )
}
fn invalid(msg: impl Into<String>) -> Fault {
    fault(422, msg)
}
fn scope(record: &Value, key: &str) -> (String, i64, String) {
    let (kind, index) = if key == "pronunciation" {
        ("word", 0)
    } else if ["definition_cn", "definition_en"].contains(&key) {
        ("sense", 1)
    } else {
        ("example", 2)
    };
    (
        kind.into(),
        record["pair"][index].as_i64().unwrap_or(0),
        key.into(),
    )
}
pub fn review_plan(c: &Connection, manifest: &Value, result: &Value) -> Result<Vec<Value>> {
    let records = manifest["records"]
        .as_array()
        .ok_or_else(|| invalid("Invalid or edited report manifest"))?;
    if manifest["format_version"] != 1
        || manifest["task"] != "content_reports"
        || records.is_empty()
        || records.len() > 1000
        || digest(&manifest["records"]) != s(manifest, "batch")
    {
        return Err(invalid("Invalid or edited report manifest"));
    }
    let mut record_map = BTreeMap::new();
    for rec in records {
        if !rec.is_object()
            || n(rec, "id") <= 0
            || !rec["reported_content"].is_object()
            || (!rec["expected_content"].is_object() && !rec["expected_content"].is_null())
            || !rec["editable"].is_boolean()
            || rec["pair"]
                .as_array()
                .is_none_or(|p| p.len() != 3 || p.iter().any(|v| v.as_i64().is_none_or(|v| v <= 0)))
            || rec.get("category").is_none()
            || rec.get("details").is_none()
        {
            return Err(invalid("Invalid report record"));
        }
        if record_map.insert(n(rec, "id"), rec).is_some() {
            return Err(invalid(
                "Duplicate report ID or result belongs to another batch",
            ));
        }
    }
    if result["batch"] != manifest["batch"] {
        return Err(invalid(
            "Duplicate report ID or result belongs to another batch",
        ));
    }
    let input = result["items"]
        .as_array()
        .ok_or_else(|| invalid("Every report ID must appear exactly once"))?;
    let mut ids = BTreeSet::new();
    let mut items = Vec::new();
    let mut intents = BTreeMap::new();
    for item in input {
        let id = n(item, "id");
        if !record_map.contains_key(&id) || !ids.insert(id) {
            return Err(invalid("Every report ID must appear exactly once"));
        }
        let mut changes = json!({});
        for ch in item["changes"]
            .as_array()
            .ok_or_else(|| invalid("Invalid correction fields"))?
        {
            let k = s(ch, "field");
            if !FIELDS.contains(&k) || changes.get(k).is_some() {
                return Err(invalid(format!(
                    "Invalid notes or correction fields for report {id}"
                )));
            }
            changes[k] = ch["value"].clone();
        }
        if s(item, "notes").trim().is_empty() {
            return Err(invalid(format!(
                "Invalid notes or correction fields for report {id}"
            )));
        }
        if (s(item, "action") == "correct") != !changes.as_object().unwrap().is_empty() {
            return Err(invalid(format!(
                "Only correct actions may include changes: {id}"
            )));
        }
        let changes = normalized(&changes);
        for (k, v) in changes.as_object().unwrap() {
            let target = scope(record_map[&id], k);
            if intents.get(&target).is_some_and(|old| old != v) {
                return Err(invalid(format!(
                    "Conflicting corrections in this batch: {target:?}"
                )));
            }
            intents.insert(target, v.clone());
        }
        items.push(json!({"id":id,"action":item["action"],"changes":changes,"notes":s(item,"notes").trim()}));
    }
    if ids.len() != records.len() {
        return Err(invalid("Every report ID must appear exactly once"));
    }
    let mut plan = Vec::new();
    for mut item in items {
        let id = n(&item, "id");
        let record = record_map[&id];
        let live = detail(c, id)?;
        let p = pair(c, id)?;
        if json!([p["word_id"], p["sense_id"], p["example_id"]]) != record["pair"]
            || live["content"] != record["reported_content"]
            || live["category"] != record["category"]
            || live["details"] != record["details"]
        {
            return Err(invalid(format!("Report source changed: {id}")));
        }
        let mut expected = record["expected_content"].clone();
        if expected.is_object() {
            for k in FIELDS {
                if let Some(v) = intents.get(&scope(record, k)) {
                    expected[k] = v.clone();
                }
            }
        }
        let state = if live["status"] == "resolved" {
            if live["current_content"] == expected && live["resolution_notes"] == item["notes"] {
                "already_applied"
            } else {
                return Err(invalid(format!("Report already handled: {id}")));
            }
        } else if item["action"] == "manual" {
            "manual"
        } else {
            if live["current_content"] != record["expected_content"]
                || live["editable"] != record["editable"]
            {
                return Err(invalid(format!("Content changed since export: {id}")));
            }
            if item["action"] == "correct" {
                if !b(&live, "editable") {
                    return Err(invalid(format!("Content archived: {id}")));
                }
                let (_, after) = checked(&live["current_content"], &item["changes"])?;
                if !scalar(
                    c,
                    "SELECT 1 FROM sense_examples WHERE sense_id=? AND sentence=? AND id!=?",
                    &[
                        p["sense_id"].clone(),
                        after["sentence"].clone(),
                        p["example_id"].clone(),
                    ],
                )?
                .is_null()
                {
                    return Err(invalid(format!("Duplicate example: {id}")));
                }
            }
            "ready"
        };
        item["state"] = json!(state);
        plan.push(item);
    }
    Ok(plan)
}
pub fn backup(c: &Connection) -> Result<String> {
    let source = c
        .path()
        .ok_or_else(|| fault(500, "Database path missing"))?;
    let path = format!(
        "{source}.bak-report-review-{}",
        Utc::now().format("%Y%m%dT%H%M%S%fZ")
    );
    c.backup("main", &path, None)?;
    Ok(path)
}
pub fn import(c: &mut Connection, config: &Config, r: &Request) -> Result<Reply> {
    let user = accounts::authenticate(c, config, r)?;
    if s(&user, "role") != "admin" {
        return Err(fault(403, "Administrator access required"));
    }
    import_as(c, config, r, &user)
}
pub fn import_as(c: &mut Connection, config: &Config, r: &Request, user: &Value) -> Result<Reply> {
    if scalar(
        c,
        "SELECT 1 FROM users WHERE id=? AND role='admin'",
        &[user["id"].clone()],
    )?
    .is_null()
    {
        return Err(fault(403, "Administrator access required"));
    }
    let dry = r
        .query
        .get("dry_run")
        .is_none_or(|v| !["false", "0", "no", "off"].contains(&v.to_ascii_lowercase().as_str()));
    let manifest = &r.body["manifest"];
    let result = &r.body["result"];
    let plan = review_plan(c, manifest, result)?;
    let ready = plan
        .iter()
        .filter(|i| i["state"] == "ready")
        .collect::<Vec<_>>();
    let mut reply = json!({"corrected":ready.iter().filter(|i|i["action"]=="correct").count(),"confirmed":ready.iter().filter(|i|i["action"]=="no_change").count(),"manual":plan.iter().filter(|i|i["state"]=="manual").count(),"already_applied":plan.iter().filter(|i|i["state"]=="already_applied").count(),"dry_run":dry,"backup":null,"items":plan});
    if dry || ready.is_empty() {
        return Ok(reply.into());
    }
    let backup = backup(c)?;
    c.execute_batch("BEGIN IMMEDIATE")?;
    let outcome = (|| {
        let plan = review_plan(c, manifest, result)?;
        for item in &plan {
            if item["state"] != "ready" {
                continue;
            }
            let id = n(item, "id");
            let live = detail(c, id)?;
            if item["action"] == "correct"
                && item["changes"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .any(|(k, v)| live["current_content"][k] != *v)
            {
                correct(
                    c,
                    config,
                    id,
                    &json!({"expected_content":live["current_content"],"changes":item["changes"],"resolution_notes":item["notes"]}),
                    n(user, "id"),
                )?;
            } else {
                exec(
                    c,
                    "UPDATE content_reports SET status='resolved',resolution_notes=?,resolved_at=?,resolved_by=? WHERE id=?",
                    &[
                        item["notes"].clone(),
                        json!(config.stamp()),
                        user["id"].clone(),
                        json!(id),
                    ],
                )?;
            }
        }
        if !rows(c, "PRAGMA foreign_key_check", &[])?.is_empty() {
            return Err(invalid("Database integrity check failed"));
        }
        Ok(())
    })();
    match outcome {
        Ok(()) => {
            c.execute_batch("COMMIT")?;
            reply["backup"] = json!(backup);
            Ok(reply.into())
        }
        Err(e) => {
            let _ = c.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}
pub fn handle(c: &Connection, config: &Config, r: &Request, user: &Value) -> Result<Value> {
    let path = r.path.trim_start_matches("/api/admin/");
    match (r.method.as_str(), path) {
        ("GET", "overview") => {
            let (start, end) = today(user, config)?;
            let mut activity = one(
                c,
                sql("overview/0"),
                &[json!(start), json!(end), json!(start), json!(end)],
            )?
            .unwrap();
            for (key, index) in [("users", 1), ("public_words", 2), ("pending_reports", 3)] {
                activity[key] = scalar(c, sql(&format!("overview/{index}")), &[])?;
            }
            Ok(activity)
        }
        ("GET", "users") => {
            let (offset, limit) = (
                page(r, "offset", 0, 0, i64::MAX)?,
                page(r, "limit", 50, 1, 100)?,
            );
            Ok(
                json!({"users":rows(c,sql("users/1"),&[json!(limit),json!(offset)])?,"total":scalar(c,sql("users/0"),&[])?,"offset":offset,"limit":limit}),
            )
        }
        ("GET", p) if p.starts_with("users/") => {
            let id = p[6..]
                .parse::<i64>()
                .map_err(|_| fault(422, "Invalid user ID"))?;
            let learner = one(c, sql("user_detail/0"), &[json!(id)])?
                .ok_or_else(|| fault(404, "用户不存在"))?;
            let (start, end) = today(&learner, config)?;
            let mut activity = one(
                c,
                sql("user_detail/1"),
                &[json!(start), json!(end), json!(id)],
            )?
            .unwrap();
            let states = rows(c, sql("user_detail/2"), &[json!(id)])?;
            activity["learned_words"] =
                scalar(c, sql("user_detail/3"), &[json!(id), json!(id), json!(id)])?;
            for (key, status) in [
                ("learning_senses", "Learning"),
                ("reviewing_senses", "Reviewing"),
                ("mature_senses", "Mature"),
            ] {
                activity[key] = states
                    .iter()
                    .find(|v| v["status"] == status)
                    .map(|v| v["total"].clone())
                    .unwrap_or(json!(0));
            }
            activity["muted_words"] = scalar(c, sql("user_detail/5"), &[json!(id)])?;
            activity["passkeys"] = scalar(c, sql("user_detail/6"), &[json!(id)])?;
            Ok(
                json!({"user":learner,"activity":activity,"word_lists":rows(c,sql("user_detail/4"),&[json!(id)])?}),
            )
        }
        ("GET", "reports") => {
            let (offset, limit) = (
                page(r, "offset", 0, 0, i64::MAX)?,
                page(r, "limit", 50, 1, 100)?,
            );
            let status = r
                .query
                .get("status")
                .map(String::as_str)
                .unwrap_or("pending");
            if !["pending", "resolved"].contains(&status) {
                return Err(fault(422, "Invalid report status"));
            }
            Ok(
                json!({"reports":rows(c,sql("reports/1"),&[json!(status),json!(limit),json!(offset)])?,"total":scalar(c,sql("reports/0"),&[json!(status)])?,"offset":offset,"limit":limit}),
            )
        }
        ("GET", "report-review/export") => {
            let manifest = export(
                c,
                page(r, "limit", 100, 1, 1000)?,
                page(r, "after_id", 0, 0, i64::MAX)?,
            )?;
            let mut reference: Value =
                serde_json::from_str(include_str!("../../../../fixtures/report-review.json"))
                    .expect("frozen report exchange");
            reference["manifest"] = manifest.clone();
            reference["result_schema"]["properties"]["batch"]["const"] = manifest["batch"].clone();
            Ok(reference)
        }
        ("POST", "report-review/validate") => {
            Ok(json!({"items":review_plan(c,&r.body["manifest"],&r.body["result"])?}))
        }
        (method, p) if p.starts_with("reports/") => {
            let mut parts = p[8..].split('/');
            let id = parts
                .next()
                .unwrap_or("")
                .parse::<i64>()
                .map_err(|_| fault(422, "Invalid report ID"))?;
            match (method, parts.next()) {
                ("GET", None) => detail(c, id),
                ("PATCH", Some("content")) => correct(c, config, id, &r.body, n(user, "id")),
                ("PATCH", None) => {
                    let current = detail(c, id)?;
                    let notes = if r.body["resolution_notes"].is_null() {
                        current["resolution_notes"].clone()
                    } else {
                        json!(s(&r.body, "resolution_notes").trim())
                    };
                    if r.body["status"] == "resolved" {
                        let stamp = if current["status"] == "resolved" {
                            current["resolved_at"].clone()
                        } else {
                            json!(config.stamp())
                        };
                        exec(
                            c,
                            "UPDATE content_reports SET status='resolved',resolution_notes=?,resolved_at=?,resolved_by=? WHERE id=?",
                            &[notes, stamp, user["id"].clone(), json!(id)],
                        )?;
                    } else {
                        exec(
                            c,
                            "UPDATE content_reports SET status='pending',resolution_notes=?,resolved_at=NULL,resolved_by=NULL WHERE id=?",
                            &[notes, json!(id)],
                        )?;
                    }
                    detail(c, id)
                }
                _ => Err(fault(404, "Not Found")),
            }
        }
        _ => Err(fault(404, "Not Found")),
    }
}
