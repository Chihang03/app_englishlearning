//! One-word-at-a-time catalogue serialization, compatible with meaning_catalog.py.
pub mod fingerprint;
pub mod pipeline;
use anyhow::{Context, Result, bail};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    io::{self, Write},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

// Keep this query's ordering and columns aligned with Python's catalogue contract.
const SQL: &str = r#"
SELECT w.id, w.word, s.id, b.learning_unit_id, s.part_of_speech, s.definition_cn, s.definition_en,
 CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN u.display_definition_cn END,
 CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN u.display_definition_en END,
 CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN json_extract(u.provenance_json,'$.notes') END,
 e.id, e.sentence, e.translation_cn, e.target_form
FROM words w JOIN word_senses s ON s.word_id=w.id AND s.active=1
JOIN learning_unit_senses b ON b.sense_id=s.id JOIN learning_units u ON u.id=b.learning_unit_id
LEFT JOIN sense_examples e ON e.sense_id=s.id AND e.active=1
WHERE w.owner_id IS NULL OR w.owner_id=?
ORDER BY w.id, s.position, s.id, e.id
"#;

#[derive(Serialize)]
struct Word {
    id: i64,
    word: String,
    senses: Vec<Sense>,
}
#[derive(Serialize)]
struct Sense {
    id: i64,
    learning_unit_id: i64,
    part_of_speech: String,
    definition_cn: String,
    definition_en: Option<String>,
    core_definition_cn: Option<String>,
    core_definition_en: Option<String>,
    semantic_notes: Option<String>,
    examples: Vec<Example>,
}
#[derive(Serialize)]
struct Example {
    id: i64,
    sentence: String,
    translation_cn: Option<String>,
    target_form: String,
}

#[derive(Debug, Serialize)]
pub struct CatalogResult {
    pub database_id: String,
    pub revision: i64,
    pub words: u64,
    pub bytes: u64,
    pub sha256: String,
}

struct HashWriter<W> {
    inner: W,
    digest: Sha256,
    bytes: u64,
}
impl<W: Write> Write for HashWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.bytes.saturating_add(bytes.len() as u64) > 256 * 1024 * 1024 {
            return Err(io::Error::other("Catalogue exceeds 256 MiB"));
        }
        let n = self.inner.write(bytes)?;
        self.digest.update(&bytes[..n]);
        self.bytes += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Caller owns the output temp file and atomic publication. This function never writes the database.
pub fn write_catalog<W: Write>(
    database: &Path,
    user_id: i64,
    expected_database_id: &str,
    expected_revision: i64,
    writer: W,
    budget: Duration,
) -> Result<CatalogResult> {
    if user_id <= 0 {
        bail!("user id must be positive");
    }
    let deadline = Instant::now()
        .checked_add(budget)
        .context("invalid deadline")?;
    let conn = english_storage::open_readonly(database)?;
    let timed_out = Arc::new(AtomicBool::new(false));
    let flag = timed_out.clone();
    conn.progress_handler(
        1000,
        Some(move || {
            let expired = Instant::now() >= deadline;
            flag.store(expired, Ordering::Relaxed);
            expired
        }),
    );
    conn.execute_batch("BEGIN")?;
    let (db_id, revision): (String, i64) = conn.query_row(
        "SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if db_id != expected_database_id || revision != expected_revision {
        bail!("catalogue revision changed");
    }
    let learner: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id=? AND role='learner')",
        [user_id],
        |r| r.get(0),
    )?;
    if !learner {
        bail!("learner account does not exist");
    }
    let mut out = HashWriter {
        inner: writer,
        digest: Sha256::new(),
        bytes: 0,
    };
    out.write_all(b"{\"words\":[")?;
    let mut statement = conn.prepare(SQL)?;
    let mut rows = statement.query([user_id])?;
    let mut current: Option<Word> = None;
    let mut count = 0;
    // Bound a pathological single word as well as the total document. Normal content is much smaller.
    let mut word_bytes = 0usize;
    while let Some(row) = rows.next()? {
        if Instant::now() >= deadline {
            bail!("catalogue deadline exceeded");
        }
        let id: i64 = row.get(0)?;
        if current.as_ref().is_none_or(|w| w.id != id) {
            if let Some(word) = current.take() {
                if count > 0 {
                    out.write_all(b",")?;
                }
                serde_json::to_writer(&mut out, &word)?;
                count += 1;
            }
            current = Some(Word {
                id,
                word: row.get(1)?,
                senses: Vec::new(),
            });
            word_bytes = 0;
        }
        let word = current.as_mut().context("missing current word")?;
        let sense_id: i64 = row.get(2)?;
        if word.senses.last().is_none_or(|s| s.id != sense_id) {
            if word.senses.len() >= 1024 {
                bail!("too many senses in one word");
            }
            let sense = Sense {
                id: sense_id,
                learning_unit_id: row.get(3)?,
                part_of_speech: row.get(4)?,
                definition_cn: row.get(5)?,
                definition_en: row.get(6)?,
                core_definition_cn: row.get(7)?,
                core_definition_en: row.get(8)?,
                semantic_notes: row.get(9)?,
                examples: Vec::new(),
            };
            word_bytes += sense.part_of_speech.len()
                + sense.definition_cn.len()
                + sense.definition_en.as_ref().map_or(0, String::len)
                + sense.core_definition_cn.as_ref().map_or(0, String::len)
                + sense.core_definition_en.as_ref().map_or(0, String::len)
                + sense.semantic_notes.as_ref().map_or(0, String::len);
            word.senses.push(sense);
        }
        if let Some(id) = row.get::<_, Option<i64>>(10)? {
            let sense = word.senses.last_mut().context("missing current sense")?;
            if sense.examples.len() >= 4096 {
                bail!("too many examples in one sense");
            }
            let example = Example {
                id,
                sentence: row.get(11)?,
                translation_cn: row.get(12)?,
                target_form: row.get(13)?,
            };
            word_bytes += example.sentence.len()
                + example.translation_cn.as_ref().map_or(0, String::len)
                + example.target_form.len();
            sense.examples.push(example);
        }
        if word_bytes > 2 * 1024 * 1024 {
            bail!("catalogue word exceeds memory budget");
        }
    }
    if let Some(word) = current {
        if count > 0 {
            out.write_all(b",")?;
        }
        serde_json::to_writer(&mut out, &word)?;
        count += 1;
    }
    out.write_all(b"]}")?;
    out.flush()?;
    Ok(CatalogResult {
        database_id: db_id,
        revision,
        words: count,
        bytes: out.bytes,
        sha256: format!("{:x}", out.digest.finalize()),
    })
}
