//! Development-only bounded SQL plan profiler. Not shipped in production releases.
use anyhow::{Context, Result, ensure};
use std::time::{Duration, Instant};
fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(args.len() == 4, "database, sql-file, user-id required");
    let mut c = english_storage::open_readonly(std::path::Path::new(&args[1]))?;
    let stop = Instant::now() + Duration::from_secs(15);
    c.progress_handler(1000, Some(move || Instant::now() > stop));
    if args[2] == "api" {
        let request = english_storage::api::Request {
            method: "GET".into(),
            path: "/api/word-lists".into(),
            query: Default::default(),
            body: serde_json::json!({}),
            token: Some("rust-internal-profile".into()),
            origin: None,
            cookies: Default::default(),
        };
        let start = Instant::now();
        let result = english_storage::api::handle(
            &mut c,
            &english_storage::api::Config::from_env(None),
            &request,
        );
        match result {
            Ok(reply) => println!(
                "{}",
                serde_json::json!({"status":200,"seconds":start.elapsed().as_secs_f64(),"lists":reply.body["lists"].as_array().map(Vec::len)})
            ),
            Err(error) => println!(
                "{}",
                serde_json::json!({"status":error.status,"detail":error.detail,"seconds":start.elapsed().as_secs_f64()})
            ),
        };
        return Ok(());
    }
    let sql = std::fs::read_to_string(&args[2])?;
    ensure!(sql.len() < 65536, "SQL too large");
    let user = args[3].parse::<i64>()?;
    let deadline = Instant::now() + Duration::from_secs(15);
    c.progress_handler(1000, Some(move || Instant::now() > deadline));
    c.execute_batch("BEGIN")?;
    let mut plan = c.prepare(&format!("EXPLAIN QUERY PLAN {sql}"))?;
    for index in 1..=plan.parameter_count() {
        plan.raw_bind_parameter(index, user)?;
    }
    let mut rows = plan.raw_query();
    let mut plans = Vec::new();
    while let Some(row) = rows.next()? {
        plans.push(row.get::<_, String>(3)?);
    }
    drop(rows);
    drop(plan);
    let start = Instant::now();
    let mut statement = c.prepare(&sql)?;
    for index in 1..=statement.parameter_count() {
        statement.raw_bind_parameter(index, user)?;
    }
    let mut rows = statement.raw_query();
    let mut count = 0;
    while rows.next().context("profile query")?.is_some() {
        count += 1;
    }
    println!(
        "{}",
        serde_json::json!({"plan":plans,"seconds":start.elapsed().as_secs_f64(),"rows":count})
    );
    Ok(())
}
