use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use std::{
    io::{BufWriter, Read},
    path::PathBuf,
    time::Duration,
};

#[derive(Parser)]
#[command(version, about = "Bounded native tools for Context Vocabulary Trainer")]
struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Create an empty v22 database; refuses every existing path.
    Init {
        #[arg(long)]
        database: PathBuf,
    },
    /// Verify schema, integrity and aggregate row counts.
    Inspect {
        #[arg(long)]
        database: PathBuf,
    },
    /// Consistent SQLite backup, including committed WAL contents.
    Backup {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// Provision the reserved administrator without changing existing credentials.
    CreateAdmin {
        #[arg(long)]
        database: PathBuf,
        #[arg(long, required = true)]
        password_stdin: bool,
    },
    /// Review exchange as an existing administrator, using bounded JSON files.
    ReportReview {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        admin_id: i64,
        #[command(subcommand)]
        action: ReportAction,
    },

    /// Run one bounded, deduplicated notification delivery pass.
    Reminders {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        now: Option<chrono::DateTime<chrono::Utc>>,
    },
    /// Legacy Python-compatible canonical JSON hash; documents are streamed.
    Fingerprint {
        #[arg(required=true,num_args=1..)]
        inputs: Vec<PathBuf>,
    },
    /// Build, validate or atomically apply an optimistic content package.
    Content {
        #[command(subcommand)]
        command: ContentCommand,
    },
    /// Export a fixed account-visible catalogue; never mutate the source database.
    Catalog {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        user_id: i64,
        #[arg(long)]
        database_id: String,
        #[arg(long)]
        revision: i64,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value_t = 30, value_parser = clap::value_parser!(u64).range(1..=120))]
        timeout_seconds: u64,
    },
    /// Check a single frozen FSRS calculation (JSON in/out), without learning writes.
    FsrsProbe {
        #[arg(long)]
        input: PathBuf,
    },
}

#[derive(Subcommand)]
enum ReportAction {
    Export {
        #[arg(long)]
        output: PathBuf,
        #[arg(long,default_value_t=100,value_parser=clap::value_parser!(i64).range(1..=1000))]
        limit: i64,
        #[arg(long, default_value_t = 0)]
        after_id: i64,
    },
    Validate {
        #[arg(long)]
        input: PathBuf,
    },
    Import {
        #[arg(long)]
        input: PathBuf,
        #[arg(long)]
        dry_run: bool,
    },
}
fn read_bounded(path: &std::path::Path) -> Result<serde_json::Value> {
    ensure!(
        std::fs::metadata(path)?.len() <= 2097152,
        "Input exceeds 2 MiB"
    );
    Ok(serde_json::from_reader(std::fs::File::open(path)?)?)
}
fn publish_json(path: &std::path::Path, value: &serde_json::Value) -> Result<()> {
    let mut temp = tempfile::NamedTempFile::new_in(path.parent().context("Output parent")?)?;
    serde_json::to_writer(&mut temp, value)?;
    temp.as_file().sync_all()?;
    temp.persist_noclobber(path)?;
    Ok(())
}

#[derive(Subcommand)]
enum ContentCommand {
    ExportSeed {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    Diff {
        #[arg(long)]
        before: PathBuf,
        #[arg(long)]
        after: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    Build {
        #[arg(long)]
        source: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    Validate {
        #[arg(long)]
        package: PathBuf,
    },
    Apply {
        #[arg(long)]
        package: PathBuf,
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        dry_run: bool,
    },
}

fn main() -> Result<()> {
    match Args::parse().command {
        Command::Init { database } => {
            println!("{}", english_storage::operations::initialize(&database)?)
        }
        Command::Inspect { database } => {
            println!("{}", english_storage::operations::inspect(&database)?)
        }
        Command::Backup { database, output } => println!(
            "{}",
            english_storage::operations::backup(&database, &output)?
        ),
        Command::CreateAdmin {
            database,
            password_stdin: _,
        } => {
            use std::io::BufRead;
            let mut password = String::new();
            std::io::stdin()
                .lock()
                .take(1024)
                .read_line(&mut password)?;
            let password = password.trim_end_matches(['\r', '\n']);
            println!(
                "{}",
                english_storage::operations::create_admin(
                    &database,
                    password,
                    &english_storage::api::Config::from_env(None)
                )?
            );
        }
        Command::ReportReview {
            database,
            admin_id,
            action,
        } => {
            let result = match action {
                ReportAction::Export {
                    output,
                    limit,
                    after_id,
                } => {
                    let value = english_storage::operations::report(
                        &database, admin_id, "export", None, true, limit, after_id,
                    )?;
                    publish_json(&output, &value)?;
                    serde_json::json!({"status":"exported","records":value["manifest"]["records"].as_array().map(Vec::len)})
                }
                ReportAction::Validate { input } => english_storage::operations::report(
                    &database,
                    admin_id,
                    "validate",
                    Some(read_bounded(&input)?),
                    true,
                    100,
                    0,
                )?,
                ReportAction::Import { input, dry_run } => english_storage::operations::report(
                    &database,
                    admin_id,
                    "import",
                    Some(read_bounded(&input)?),
                    dry_run,
                    100,
                    0,
                )?,
            };
            println!("{result}");
        }

        Command::Reminders {
            database,
            dry_run,
            now,
        } => println!(
            "{}",
            serde_json::to_string(&english_storage::reminders::run(
                &database,
                now.unwrap_or_else(chrono::Utc::now),
                dry_run
            )?)?
        ),
        Command::Fingerprint { inputs } => println!(
            "{}",
            serde_json::to_string(
                &serde_json::json!({"sha256":english_content::fingerprint::fingerprint(&inputs)?})
            )?
        ),
        Command::Content { command } => {
            let result = match command {
                ContentCommand::ExportSeed { database, output } => {
                    english_content::pipeline::export_seed(&database, &output)?
                }
                ContentCommand::Diff {
                    before,
                    after,
                    output,
                } => {
                    serde_json::json!({"records":english_content::pipeline::diff(&before,&after,&output)?})
                }
                ContentCommand::Build { source, output } => {
                    serde_json::to_value(english_content::pipeline::build(&source, &output)?)?
                }
                ContentCommand::Validate { package } => {
                    serde_json::to_value(english_content::pipeline::validate(&package)?)?
                }
                ContentCommand::Apply {
                    package,
                    database,
                    dry_run,
                } => serde_json::to_value(english_content::pipeline::apply(
                    &package, &database, dry_run,
                )?)?,
            };
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Catalog {
            database,
            user_id,
            database_id,
            revision,
            output,
            timeout_seconds,
        } => {
            ensure!(!output.exists(), "output already exists");
            let parent = output.parent().context("output parent missing")?;
            let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
            let result = english_content::write_catalog(
                &database,
                user_id,
                &database_id,
                revision,
                BufWriter::with_capacity(65536, &mut temporary),
                Duration::from_secs(timeout_seconds),
            )?;
            temporary.as_file().sync_all()?;
            temporary
                .persist_noclobber(&output)
                .context("publish output")?;
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::FsrsProbe { input } => {
            ensure!(
                std::fs::metadata(&input)?.len() <= 65536,
                "probe input too large"
            );
            let value: serde_json::Value = serde_json::from_reader(std::fs::File::open(input)?)?;
            let card = serde_json::from_value(value["card"].clone())?;
            let now = serde_json::from_value(value["now"].clone())?;
            let rating: u8 = serde_json::from_value(value["rating"].clone())?;
            let retention: f64 = serde_json::from_value(value["retention"].clone())?;
            println!(
                "{}",
                serde_json::to_string(&english_core::fsrs_compat::review(
                    &card, rating, now, retention
                )?)?
            );
        }
    }
    Ok(())
}
