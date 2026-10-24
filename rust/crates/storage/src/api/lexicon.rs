use super::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::OnceLock,
};

pub fn sql(name: &str) -> &'static str {
    static SQL: OnceLock<Value> = OnceLock::new();
    SQL.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../fixtures/sql.json")).expect("frozen SQL")
    })[name]
        .as_str()
        .expect("known SQL")
}
pub fn filtered(name: &str, user: i64, unit: &str) -> String {
    sql(name)
        .replace("__USER__", &user.to_string())
        .replace("__UNIT__", unit)
}
pub fn scalar(c: &Connection, sql: &str, args: &[Value]) -> Result<Value> {
    Ok(one(c, sql, args)?
        .and_then(|v| v.as_object()?.values().next().cloned())
        .unwrap_or(Value::Null))
}
pub fn senses(c: &Connection, word: i64, user: i64) -> Result<Vec<Value>> {
    let mut result = rows(c, sql("senses_for_word/0"), &[json!(user), json!(word)])?;
    let mut examples = BTreeMap::<i64, Vec<Value>>::new();
    for example in rows(
        c,
        "SELECT e.* FROM sense_examples e JOIN word_senses s ON s.id=e.sense_id WHERE s.word_id=? AND s.active=1 AND e.active=1 ORDER BY e.sense_id,e.id",
        &[json!(word)],
    )? {
        examples
            .entry(n(&example, "sense_id"))
            .or_default()
            .push(example);
    }
    for sense in &mut result {
        sense["examples"] = json!(examples.remove(&n(sense, "id")).unwrap_or_default());
    }
    Ok(result)
}

pub fn sense_ids(c: &Connection, word: i64) -> Result<Vec<Value>> {
    rows(
        c,
        "SELECT s.id FROM word_senses s JOIN learning_unit_senses b ON b.sense_id=s.id JOIN learning_units u ON u.id=b.learning_unit_id WHERE s.word_id=? AND s.active=1 ORDER BY s.position,s.id",
        &[json!(word)],
    )
}

fn unit_sense_ids(
    c: &Connection,
    unit: &Value,
    scope: Option<&BTreeSet<i64>>,
) -> Result<Vec<Value>> {
    let Some(word) = unit["word_id"].as_i64() else {
        return Ok(Vec::new());
    };
    Ok(rows(c,"SELECT s.id FROM word_senses s JOIN learning_unit_senses b ON b.sense_id=s.id JOIN learning_units u ON u.id=b.learning_unit_id WHERE s.word_id=? AND s.lexical_unit_id=? AND s.active=1 ORDER BY s.position,s.id",&[json!(word),unit["id"].clone()])?.into_iter().filter(|s|scope.is_none_or(|ids|ids.contains(&n(s,"id")))).collect())
}

pub fn entry_sense_ids(c: &Connection, spelling: &str, user: i64) -> Result<Vec<Value>> {
    let normalized = fold(spelling.trim());
    rows(
        c,
        r#"WITH matches AS MATERIALIZED (
        SELECT id,word_id,NULL AS scope_sense_id FROM lexical_units
        WHERE normalized_headword=? AND active=1 AND (owner_id IS NULL OR owner_id=?)
        UNION ALL
        SELECT u.id,u.word_id,f.scope_sense_id FROM word_forms f JOIN lexical_units u ON u.id=f.lexical_unit_id
        WHERE f.normalized_form=? AND f.active=1 AND f.verification='verified'
        AND u.active=1 AND u.identity_status='verified' AND (u.owner_id IS NULL OR u.owner_id=?)
    ) SELECT DISTINCT s.id FROM matches u
      JOIN word_senses s ON s.word_id=u.word_id AND s.lexical_unit_id=u.id
      JOIN learning_unit_senses b ON b.sense_id=s.id JOIN learning_units lu ON lu.id=b.learning_unit_id
      WHERE s.active=1 AND (u.scope_sense_id IS NULL OR u.scope_sense_id=s.id)"#,
        &[
            json!(normalized),
            json!(user),
            json!(normalized),
            json!(user),
        ],
    )
}

pub fn morphology(c: &Connection, unit: Option<i64>, user: i64) -> Result<Value> {
    let empty = json!({"inflected_forms":[],"derived_words":[],"related_words":[]});
    let Some(unit) = unit else {
        return Ok(empty);
    };
    if one(
        c,
        "SELECT * FROM lexical_units WHERE id=? AND active=1 AND (owner_id IS NULL OR owner_id=?)",
        &[json!(unit), json!(user)],
    )?
    .is_none()
    {
        return Ok(empty);
    }
    let forms = rows(
        c,
        "SELECT spelling,form_type,scope_sense_id FROM word_forms WHERE lexical_unit_id=? AND active=1 AND verification='verified' ORDER BY normalized_form,id",
        &[json!(unit)],
    )?;
    let mut groups: Vec<(String, Value, Vec<String>)> = Vec::new();
    let mut positions = BTreeMap::new();
    for form in forms {
        let spelling = s(&form, "spelling").to_owned();
        let scope = form["scope_sense_id"].clone();
        let kind = s(&form, "form_type").to_owned();
        let key = (spelling.clone(), scope.as_i64());
        if let Some(&index) = positions.get(&key) {
            let item: &mut (String, Value, Vec<String>) = &mut groups[index];
            item.2.push(kind);
        } else {
            positions.insert(key, groups.len());
            groups.push((spelling, scope, vec![kind]));
        }
    }
    let forms=groups.into_iter().map(|(spelling,scope,types)|json!({"spelling":spelling,"scope_sense_id":scope,
        "label":types.iter().map(|k|form_label(k)).collect::<Vec<_>>().join("／"),"form_types":types})).collect::<Vec<_>>();
    let mut derived = Vec::new();
    let mut related = Vec::new();
    for edge in rows(
        c,
        "SELECT * FROM lexical_relations WHERE (from_unit_id=? OR to_unit_id=?) AND active=1 AND verification='verified' ORDER BY id",
        &[json!(unit), json!(unit)],
    )? {
        let target = if n(&edge, "from_unit_id") == unit {
            n(&edge, "to_unit_id")
        } else {
            n(&edge, "from_unit_id")
        };
        if let Some(peer) = one(
            c,
            "SELECT * FROM lexical_units WHERE id=? AND active=1 AND (owner_id IS NULL OR owner_id=?)",
            &[json!(target), json!(user)],
        )? {
            let learnable = scalar(c,"SELECT EXISTS(SELECT 1 FROM word_senses s JOIN learning_unit_senses b ON b.sense_id=s.id JOIN learning_units u ON u.id=b.learning_unit_id WHERE s.word_id=? AND s.lexical_unit_id=? AND s.active=1)", &[peer["word_id"].clone(),peer["id"].clone()])?.as_i64()==Some(1);
            let item = json!({"lexical_unit_id":target,"headword":peer["headword"],"pos_group":peer["pos_group"],"relation_type":edge["relation_type"],
                "direction":if n(&edge,"from_unit_id")==unit{"outgoing"}else{"incoming"},"learnable":learnable});
            if s(&edge, "relation_type").starts_with("derived_") {
                derived.push(item);
            } else {
                related.push(item);
            }
        }
    }
    Ok(json!({"inflected_forms":forms,"derived_words":derived,"related_words":related}))
}
fn unit_senses(
    c: &Connection,
    unit: &Value,
    user: i64,
    scope: Option<&BTreeSet<i64>>,
) -> Result<Vec<Value>> {
    let Some(word) = unit["word_id"].as_i64() else {
        return Ok(Vec::new());
    };
    Ok(senses(c, word, user)?
        .into_iter()
        .filter(|sense| {
            sense["lexical_unit_id"] == unit["id"]
                && scope.is_none_or(|scope| scope.contains(&n(sense, "id")))
        })
        .collect())
}
fn entry(
    c: &Connection,
    unit: &Value,
    user: i64,
    classification: &str,
    scope: Option<&BTreeSet<i64>>,
    content: bool,
) -> Result<Value> {
    let senses = if content {
        unit_senses(c, unit, user, scope)?
    } else {
        unit_sense_ids(c, unit, scope)?
    };
    Ok(
        json!({"lexical_unit_id":unit["id"],"word_id":unit["word_id"],"headword":unit["headword"],"pos_group":unit["pos_group"],"classification":classification,
        "learnable":!senses.is_empty(),"senses":senses,"morphology":morphology(c,unit["id"].as_i64(),user)?}),
    )
}
pub fn entries(c: &Connection, spelling: &str, user: i64) -> Result<Vec<Value>> {
    entries_mode(c, spelling, user, true)
}
pub fn entries_mode(
    c: &Connection,
    spelling: &str,
    user: i64,
    content: bool,
) -> Result<Vec<Value>> {
    let normalized = fold(spelling.trim());
    let mut result = Vec::new();
    for unit in rows(
        c,
        "SELECT * FROM lexical_units WHERE normalized_headword=? AND active=1 AND (owner_id IS NULL OR owner_id=?) ORDER BY owner_id IS NULL,id",
        &[json!(normalized), json!(user)],
    )? {
        let edges = rows(
            c,
            "SELECT relation_type FROM lexical_relations WHERE to_unit_id=? AND active=1 AND verification='verified'",
            &[unit["id"].clone()],
        )?;
        let classification = if edges
            .iter()
            .any(|e| s(e, "relation_type") == "lexicalized_from")
        {
            "LEXICALIZED_FORM"
        } else if edges
            .iter()
            .any(|e| s(e, "relation_type").starts_with("derived_"))
        {
            "DERIVED"
        } else if s(&unit, "identity_status") == "verified" {
            "INDEPENDENT"
        } else {
            "UNKNOWN"
        };
        result.push(entry(c, &unit, user, classification, None, content)?);
    }
    let forms = rows(
        c,
        "SELECT f.*,u.pos_group FROM word_forms f JOIN lexical_units u ON u.id=f.lexical_unit_id WHERE f.normalized_form=? AND f.active=1 AND f.verification='verified' AND u.active=1 AND u.identity_status='verified' AND (u.owner_id IS NULL OR u.owner_id=?) ORDER BY f.id",
        &[json!(normalized), json!(user)],
    )?;
    let mut grouped: Vec<(i64, Vec<Value>)> = Vec::new();
    let mut positions = BTreeMap::new();
    for form in forms {
        let id = n(&form, "lexical_unit_id");
        if let Some(&index) = positions.get(&id) {
            let item: &mut (i64, Vec<Value>) = &mut grouped[index];
            item.1.push(form);
        } else {
            positions.insert(id, grouped.len());
            grouped.push((id, vec![form]));
        }
    }
    for (id, forms) in grouped {
        let unit = one(c, "SELECT * FROM lexical_units WHERE id=?", &[json!(id)])?.unwrap();
        let scopes = forms
            .iter()
            .filter_map(|f| f["scope_sense_id"].as_i64())
            .collect::<BTreeSet<_>>();
        let scope = if forms.iter().any(|f| f["scope_sense_id"].is_null()) {
            None
        } else {
            Some(&scopes)
        };
        let mut value = entry(c, &unit, user, "INFLECTION", scope, content)?;
        let mut types = Vec::new();
        for form in forms {
            let kind = form["form_type"].clone();
            if !types.contains(&kind) {
                types.push(kind);
            }
        }
        value["matched_form"] = json!({"spelling":spelling.trim(),"form_types":types});
        result.push(value);
    }
    Ok(result)
}
pub fn form_label(kind: &str) -> &str {
    match kind {
        "first_person_singular" => "第一人称单数",
        "third_person_singular" => "第三人称单数",
        "second_person_or_plural" => "第二人称／复数",
        "present" => "现在式",
        "ing" => "-ing 形式",
        "past" => "过去式",
        "past_participle" => "过去分词",
        "plural" => "复数",
        "comparative" => "比较级",
        "superlative" => "最高级",
        _ => "",
    }
}
pub fn vocabulary_lists(c: &Connection, user: i64) -> Result<Vec<Value>> {
    let selected = rows(
        c,
        "SELECT list_id FROM user_vocabulary_lists WHERE user_id=? AND selected=1 ORDER BY list_id",
        &[json!(user)],
    )?;
    let has_preferences = one(
        c,
        "SELECT 1 FROM user_vocabulary_lists WHERE user_id=? LIMIT 1",
        &[json!(user)],
    )?
    .is_some();
    let selected = if selected.is_empty() && !has_preferences {
        rows(
            c,
            "SELECT list_id FROM vocabulary_lists WHERE default_selected=1 ORDER BY list_id",
            &[],
        )?
    } else {
        selected
    };
    let selected = selected
        .iter()
        .map(|r| s(r, "list_id").to_owned())
        .collect::<BTreeSet<_>>();
    let mut lists = rows(
        c,
        include_str!("vocabulary_progress.sql"),
        &vec![json!(user); 5],
    )?;
    for list in &mut lists {
        list["selected"] = json!(selected.contains(s(list, "list_id")));
    }
    Ok(lists)
}
pub fn settings(c: &Connection, user: i64) -> Result<Value> {
    let mut settings = BTreeMap::new();
    for item in rows(
        c,
        "SELECT key,value FROM settings WHERE user_id=?",
        &[json!(user)],
    )? {
        settings.insert(s(&item, "key").to_owned(), s(&item, "value").to_owned());
    }
    // Preferences do not require computing progress for every course.
    let lists = rows(
        c,
        "SELECT v.list_id FROM vocabulary_lists v WHERE EXISTS(SELECT 1 FROM user_vocabulary_lists p WHERE p.user_id=? AND p.list_id=v.list_id AND p.selected=1) OR (v.default_selected=1 AND NOT EXISTS(SELECT 1 FROM user_vocabulary_lists p WHERE p.user_id=?)) ORDER BY v.sort_order,v.title",
        &[json!(user), json!(user)],
    )?;
    let speech = settings
        .get("speech_rate")
        .filter(|v| ["90", "120", "175"].contains(&v.as_str()))
        .and_then(|v| v.parse::<i64>().ok());
    Ok(
        json!({"show_sentence_translation":settings.get("show_sentence_translation").is_some_and(|v|v=="true"),
        "skip_basic_600":settings.get("skip_basic_600").is_some_and(|v|v=="true"),"speech_rate":speech,
        "selected_word_list_ids":lists.iter().map(|l|l["list_id"].clone()).collect::<Vec<_>>() }),
    )
}
pub fn handle(c: &Connection, config: &Config, r: &Request, user: &Value) -> Result<Value> {
    let uid = n(user, "id");
    let path = r.path.as_str();
    let p = &r.body;
    match (r.method.as_str(), path) {
        ("GET", "/api/settings") => return settings(c, uid),
        ("GET", "/api/word-lists") => return Ok(json!({"lists":vocabulary_lists(c,uid)?})),
        ("GET", "/api/muted-words") => {
            return Ok(
                json!({"words":rows(c,"SELECT word,muted_at FROM user_muted_words WHERE user_id=? ORDER BY word COLLATE NOCASE",&[json!(uid)])?}),
            );
        }
        ("PATCH", "/api/settings") => {
            for key in ["show_sentence_translation", "skip_basic_600", "speech_rate"] {
                if p[key].is_null() {
                    continue;
                }
                let value = if key == "speech_rate" {
                    let value = integer(p, key)?;
                    if ![90, 120, 175].contains(&value) {
                        return Err(fault(422, "Invalid speech_rate"));
                    }
                    value.to_string()
                } else {
                    let value = p[key]
                        .as_bool()
                        .ok_or_else(|| fault(422, "Input should be a valid boolean"))?;
                    value.to_string()
                };
                exec(
                    c,
                    "INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
                    &[json!(uid), json!(key), json!(value)],
                )?;
                if key == "skip_basic_600" && value == "true" {
                    exec(
                        c,
                        &format!(
                            "UPDATE study_attempts SET completed_at=? WHERE user_id=? AND completed_at IS NULL AND sense_id IN (SELECT s.id FROM word_senses s JOIN words w ON w.id=s.word_id JOIN learning_unit_senses binding ON binding.sense_id=s.id WHERE {})",
                            sql("basic_unit")
                        ),
                        &[json!(config.stamp()), json!(uid)],
                    )?;
                }
            }
            if !p["selected_word_list_ids"].is_null() {
                let requested = p["selected_word_list_ids"]
                    .as_array()
                    .ok_or_else(|| fault(422, "Invalid selected_word_list_ids"))?;
                if requested.len() > 50 || requested.iter().any(|v| !v.is_string()) {
                    return Err(fault(422, "Invalid selected_word_list_ids"));
                }
                let requested = requested
                    .iter()
                    .map(|v| v.as_str().unwrap().to_owned())
                    .collect::<BTreeSet<_>>();
                let available = rows(c, "SELECT list_id FROM vocabulary_lists", &[])?
                    .iter()
                    .map(|v| s(v, "list_id").to_owned())
                    .collect::<BTreeSet<_>>();
                let unknown = requested
                    .difference(&available)
                    .cloned()
                    .collect::<Vec<_>>();
                if !unknown.is_empty() {
                    return Err(fault(
                        422,
                        format!("Unknown word lists: {}", unknown.join(", ")),
                    ));
                }
                exec(
                    c,
                    "DELETE FROM user_vocabulary_lists WHERE user_id=?",
                    &[json!(uid)],
                )?;
                for list in available {
                    exec(
                        c,
                        "INSERT INTO user_vocabulary_lists(user_id,list_id,selected) VALUES(?,?,?)",
                        &[json!(uid), json!(list), json!(requested.contains(&list))],
                    )?;
                }
            }
            return settings(c, uid);
        }
        _ => {}
    }
    if r.method == "POST" && path.starts_with("/api/words/") && path.ends_with("/mute") {
        let word_id = path
            .trim_start_matches("/api/words/")
            .trim_end_matches("/mute")
            .parse::<i64>()
            .map_err(|_| fault(422, "Invalid word id"))?;
        let word=one(c,"SELECT lower(trim(word)) AS word FROM words WHERE id=? AND (owner_id IS NULL OR owner_id=?)",&[json!(word_id),json!(uid)])?.ok_or_else(||fault(404,"单词不存在"))?;
        exec(
            c,
            "INSERT INTO user_muted_words(user_id,word,muted_at) VALUES(?,?,?) ON CONFLICT(user_id,word) DO NOTHING",
            &[json!(uid), word["word"].clone(), json!(config.stamp())],
        )?;
        exec(
            c,
            "UPDATE study_attempts SET completed_at=? WHERE user_id=? AND completed_at IS NULL AND sense_id IN (SELECT s.id FROM word_senses s JOIN words w ON w.id=s.word_id WHERE lower(trim(w.word))=?)",
            &[json!(config.stamp()), json!(uid), word["word"].clone()],
        )?;
        return Ok(json!({"word":word["word"],"muted":true}));
    }
    if r.method == "DELETE" && path.starts_with("/api/muted-words/") {
        let word = path
            .trim_start_matches("/api/muted-words/")
            .trim()
            .to_lowercase();
        exec(
            c,
            "DELETE FROM user_muted_words WHERE user_id=? AND word=?",
            &[json!(uid), json!(word)],
        )?;
        return Ok(json!({"word":word,"muted":false}));
    }
    if r.method == "GET" && path.starts_with("/api/dictionary/") {
        let word = path.trim_start_matches("/api/dictionary/");
        let entries = entries(c, word, uid)?;
        let mut definition = one(
            c,
            "SELECT word,part_of_speech,definition_cn,definition_en,example_sentence,pronunciation,id FROM words WHERE lower(word)=lower(?) AND length(trim(example_sentence))>0 AND (owner_id IS NULL OR owner_id=?) ORDER BY CASE WHEN owner_id=? THEN 0 ELSE 1 END LIMIT 1",
            &[json!(word.trim()), json!(uid), json!(uid)],
        )?;
        if let Some(value) = &mut definition {
            let senses = senses(c, n(value, "id"), uid)?;
            if senses.is_empty() {
                definition = None;
            } else {
                for key in ["part_of_speech", "definition_cn", "definition_en"] {
                    value[key] = senses[0][key].clone();
                }
                value["example_sentence"] = senses[0]["examples"][0]["sentence"].clone();
                value["senses"] = json!(senses);
            }
        }
        if definition.is_none() {
            let targets = entries
                .iter()
                .filter(|e| s(e, "classification") == "INFLECTION" && b(e, "learnable"))
                .collect::<Vec<_>>();
            if targets.len() == 1 {
                let target = targets[0];
                let primary = &target["senses"][0];
                let pronunciation = scalar(
                    c,
                    "SELECT pronunciation FROM words WHERE id=?",
                    &[target["word_id"].clone()],
                )?;
                definition = Some(
                    json!({"id":target["word_id"],"word":target["headword"],"part_of_speech":primary["part_of_speech"],"definition_cn":primary["definition_cn"],"definition_en":primary["definition_en"],"example_sentence":primary["examples"][0]["sentence"],"senses":target["senses"],"lexical_unit_id":target["lexical_unit_id"],"pronunciation":pronunciation}),
                );
            }
        }
        return Ok(
            json!({"word":word,"source":"本地词库","available":definition.is_some(),"definition":definition,"classification":if entries.is_empty(){json!("UNKNOWN")}else{Value::Null},"entries":entries}),
        );
    }
    Err(fault(404, "Not Found"))
}
