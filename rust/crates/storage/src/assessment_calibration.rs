//! Explicit import of independently validated readiness metadata. No online fit.
use anyhow::{Result, ensure};
use rusqlite::Connection;
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};
fn wilson_upper(errors: f64, n: f64) -> f64 {
    let z = 1.96;
    let p = errors / n;
    (p + z * z / (2.0 * n) + z * ((p * (1.0 - p) + z * z / (4.0 * n)) / n).sqrt())
        / (1.0 + z * z / n)
}
pub fn validate(report: &Value) -> Result<Value> {
    ensure!(
        report["model"] == english_core::assessment::MODEL,
        "Model mismatch"
    );
    ensure!(
        report["independent_reference"] == true,
        "Independent validation required"
    );
    let train = report["training_participants"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Missing training participants"))?;
    let train: BTreeSet<_> = train.iter().filter_map(Value::as_str).collect();
    ensure!(
        train.len() >= 50,
        "At least 50 training participants required"
    );
    let validation = report["validation"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Missing held-out validation"))?;
    let units = report["units"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("Missing unit metadata"))?;
    let mut people = BTreeSet::new();
    let mut pairs = BTreeSet::new();
    let mut failures = 0;
    let mut predicted = 0;
    let mut per_unit = std::collections::BTreeMap::<i64, (i32, i32)>::new();
    let ids: BTreeSet<_> = units.iter().filter_map(|u| u["unit_id"].as_i64()).collect();
    ensure!(
        ids.len() == units.len() && !ids.is_empty(),
        "Duplicate or invalid units"
    );
    for unit in units {
        ensure!(
            unit["reviewed_common_sense"] == true,
            "Only reviewed common senses may defer"
        );
        let d = unit["difficulty"]
            .as_f64()
            .ok_or_else(|| anyhow::anyhow!("Missing difficulty"))?;
        ensure!(
            d.is_finite() && (-6.0..=6.0).contains(&d),
            "Difficulty outside scale"
        );
        ensure!(
            unit["training_responses"].as_u64().unwrap_or(0) >= 100,
            "At least 100 responses per unit required"
        );
    }
    for v in validation {
        let person = v["participant"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing participant"))?;
        ensure!(!train.contains(person), "Training and validation overlap");
        people.insert(person);
        let id = v["unit_id"]
            .as_i64()
            .ok_or_else(|| anyhow::anyhow!("Invalid unit"))?;
        ensure!(
            ids.contains(&id) && pairs.insert((person, id)),
            "Invalid or duplicate validation pair"
        );
        let mean = v["mean"]
            .as_f64()
            .ok_or_else(|| anyhow::anyhow!("Missing independent ability"))?;
        let sd = v["sd"]
            .as_f64()
            .ok_or_else(|| anyhow::anyhow!("Missing uncertainty"))?;
        ensure!(
            mean.is_finite() && sd.is_finite() && sd > 0.0,
            "Invalid ability estimate"
        );
        let known = v["known"]
            .as_bool()
            .ok_or_else(|| anyhow::anyhow!("Missing reference answer"))?;
        let d = units.iter().find(|u| u["unit_id"] == id).unwrap()["difficulty"]
            .as_f64()
            .unwrap();
        let p = (english_core::assessment::probability(mean - 1.645 * sd, d) - 0.25) / 0.75;
        if p >= 0.95 {
            predicted += 1;
            let counts = per_unit.entry(id).or_default();
            counts.0 += 1;
            if !known {
                counts.1 += 1;
            }
            if !known {
                failures += 1;
            }
        }
    }
    ensure!(
        people.len() >= 30 && predicted >= 200,
        "At least 30 held-out participants and 200 deferral decisions required"
    );
    for id in &ids {
        let (total, errors) = per_unit.get(id).copied().unwrap_or_default();
        ensure!(
            total >= 100 && wilson_upper(f64::from(errors), f64::from(total)) <= 0.05,
            "Each activated unit needs 100 held-out deferrals and a false-deferral upper bound at most 5 percent"
        );
    }
    let upper = wilson_upper(f64::from(failures), f64::from(predicted));
    ensure!(
        upper <= 0.05,
        "False-deferral confidence bound exceeds 5 percent"
    );
    Ok(
        json!({"eligible":true,"participants":people.len(),"predicted_deferred":predicted,"false_deferred":failures,"false_deferred_upper95":upper}),
    )
}
pub fn import(database: &Path, report: &Value, dry_run: bool) -> Result<Value> {
    let metrics = validate(report)?;
    let version = report["version"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("Missing calibration version"))?;
    ensure!(
        !version.is_empty() && version.len() <= 80,
        "Invalid version"
    );
    let mut c = Connection::open(database)?;
    c.pragma_update(None, "foreign_keys", true)?;
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let already: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM assessment_calibrations WHERE version=?)",
        [version],
        |r| r.get(0),
    )?;
    ensure!(!already, "Calibration version already exists");
    for u in report["units"].as_array().unwrap() {
        let active: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM learning_units WHERE id=? AND active=1)",
            [u["unit_id"].as_i64()],
            |r| r.get(0),
        )?;
        ensure!(active, "Unknown active learning unit");
        tx.execute("INSERT INTO learning_unit_difficulty(learning_unit_id,difficulty,source,verified,calibration_version) VALUES(?,?,'heldout-reference',1,?) ON CONFLICT(learning_unit_id) DO UPDATE SET difficulty=excluded.difficulty,source=excluded.source,verified=1,calibration_version=excluded.calibration_version,retired=0",rusqlite::params![u["unit_id"].as_i64(),u["difficulty"].as_f64(),version])?;
    }
    tx.execute("INSERT INTO assessment_calibrations(version,report_json,created_at,active) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'),1)",rusqlite::params![version,report.to_string()])?;
    if dry_run {
        tx.rollback()?;
    } else {
        tx.commit()?;
    }
    Ok(json!({"dry_run":dry_run,"validation":metrics,"version":version}))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn heldout_gate_checks_overlap_and_false_deferrals() {
        let mut report = json!({"model":english_core::assessment::MODEL,"independent_reference":true,
            "training_participants":(0..50).map(|i|format!("t{i}")).collect::<Vec<_>>(),
            "units":[{"unit_id":1,"difficulty":-2.0,"reviewed_common_sense":true,"training_responses":100}],
            "validation":(0..200).map(|i|json!({"participant":format!("v{i}"),"unit_id":1,"mean":6.0,"sd":0.1,"known":true})).collect::<Vec<_>>()});
        assert!(validate(&report).is_ok());
        report["validation"][0]["participant"] = json!("t0");
        assert!(validate(&report).is_err());
        report["validation"][0]["participant"] = json!("v0");
        for i in 0..20 {
            report["validation"][i]["known"] = json!(false);
        }
        assert!(validate(&report).is_err());
    }
    #[test]
    fn refuses_small_or_unreferenced_validation() {
        assert!(validate(&json!({"model":english_core::assessment::MODEL})).is_err());
        assert!(wilson_upper(0.0, 10.0) > 0.05);
        assert!(wilson_upper(0.0, 200.0) < 0.05);
    }
}
