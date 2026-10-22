use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use std::{io::BufWriter, path::PathBuf, time::Duration};

#[derive(Parser)]
#[command(version, about = "Bounded native tools for Context Vocabulary Trainer")]
struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
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
enum ContentCommand {
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
        Command::Fingerprint { inputs } => println!(
            "{}",
            serde_json::to_string(
                &serde_json::json!({"sha256":english_content::fingerprint::fingerprint(&inputs)?})
            )?
        ),
        Command::Content { command } => {
            let result = match command {
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
