//! Account-scoped, content-addressed dictionary blocks. No learning state.
use super::*;
use anyhow::ensure;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::PathBuf;

#[derive(Serialize, Deserialize)]
pub struct Block {
    pub hash: String,
    pub bytes: usize,
    pub words: usize,
}
#[derive(Serialize, Deserialize)]
pub struct Manifest {
    pub format: u8,
    pub blocks: Vec<Block>,
    pub words: usize,
}

// Morphology changes are outside the legacy catalogue revision triggers.
// Stream the relevant tables rather than changing the production schema.
pub fn morphology_fingerprint(conn: &Connection) -> Result<String> {
    let mut hash = Sha256::new();
    for sql in [
        "SELECT * FROM lexical_units ORDER BY id",
        "SELECT * FROM word_forms ORDER BY id",
        "SELECT * FROM lexical_relations ORDER BY id",
        "SELECT id,lexical_unit_id FROM word_senses ORDER BY id",
    ] {
        hash.update(sql.as_bytes());
        let mut statement = conn.prepare(sql)?;
        let columns = statement.column_count();
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            for column in 0..columns {
                let value = row.get_ref(column)?;
                // Type and length boundaries avoid ambiguous concatenations.
                let bytes = format!("{value:?}");
                hash.update((bytes.len() as u64).to_le_bytes());
                hash.update(bytes.as_bytes());
            }
        }
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub(crate) struct ChunkWriter {
    directory: PathBuf,
    fingerprint: String,
    prefix: String,
    bucket: Option<i64>,
    words: Vec<Value>,
    chars: usize,
    total: usize,
    manifest: Manifest,
}
impl ChunkWriter {
    pub(super) fn verify_snapshot(&self, conn: &Connection) -> Result<()> {
        ensure!(
            morphology_fingerprint(conn)? == self.fingerprint,
            "morphology changed"
        );
        Ok(())
    }
    fn flush(&mut self) -> Result<()> {
        if self.words.is_empty() {
            return Ok(());
        }
        let bytes = serde_json::to_vec(&json!({"words":self.words}))?;
        self.total += bytes.len();
        ensure!(self.total <= 256 * 1024 * 1024, "catalogue exceeds budget");
        let hash = format!("{:x}", Sha256::digest(&bytes));
        let path = self.directory.join(format!("{}-{hash}.json", self.prefix));
        if !path.is_file() {
            let mut file = tempfile::NamedTempFile::new_in(&self.directory)?;
            file.write_all(&bytes)?;
            file.as_file().sync_all()?;
            file.persist(path)?;
        }
        self.manifest.words += self.words.len();
        self.manifest.blocks.push(Block {
            hash,
            bytes: bytes.len(),
            words: self.words.len(),
        });
        self.words.clear();
        self.chars = 0;
        Ok(())
    }
    pub(super) fn push(&mut self, conn: &Connection, user: i64, word: &Word) -> Result<()> {
        // Fixed ID ranges keep edits/deletions from shifting every later block.
        let bucket = word.id / 64;
        if self.bucket != Some(bucket) {
            self.flush()?;
            self.bucket = Some(bucket);
        }
        // This exporter streams one word at a time on its own blocking thread.
        // API row budgets must bound each word, not accumulate across the full
        // dictionary (or across separate exports reusing this worker thread).
        english_storage::api::reset_budget();
        let mut value = serde_json::to_value(word)?;
        let mut statement = conn.prepare("SELECT id,headword,pos_group FROM lexical_units WHERE word_id=? AND active=1 AND (owner_id IS NULL OR owner_id=?) ORDER BY id")?;
        let mut rows = statement.query([word.id, user])?;
        let mut entries = Vec::new();
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let metadata = english_storage::api::lexicon::morphology(conn, Some(id), user)
                .map_err(|e| anyhow::anyhow!("morphology: {}", e.detail))?;
            entries.push(
                json!({"lexical_unit_id":id, "headword":row.get::<_,String>(1)?,
                "pos_group":row.get::<_,String>(2)?, "morphology":metadata}),
            );
        }
        value["morphology_entries"] = json!(entries);
        let size = serde_json::to_vec(&value)?.len();
        ensure!(size <= 2 * 1024 * 1024, "word exceeds budget");
        if self.chars + size > 1024 * 1024 {
            self.flush()?;
        }
        self.chars += size;
        self.words.push(value);
        Ok(())
    }
}

pub fn write_chunks(
    database: &Path,
    user: i64,
    identity: &str,
    revision: i64,
    directory: &Path,
    fingerprint: &str,
) -> Result<Manifest> {
    let mut chunks = ChunkWriter {
        directory: directory.into(),
        fingerprint: fingerprint.into(),
        prefix: format!("v3-block-{identity}-{user}"),
        bucket: None,
        words: Vec::new(),
        chars: 0,
        total: 0,
        manifest: Manifest {
            format: 1,
            words: 0,
            blocks: Vec::new(),
        },
    };
    write_catalog_inner(
        database,
        user,
        identity,
        revision,
        io::sink(),
        Duration::from_secs(30),
        Some(&mut chunks),
    )?;
    chunks.flush()?;
    Ok(chunks.manifest)
}
