//! Bounded, optimistic content patches. The entire package is validated before a target is opened for writes.
use anyhow::{Context, Result, bail, ensure};
use rusqlite::{Connection, OptionalExtension, params, params_from_iter, types::Value as SqlValue};
use serde::{
    Deserialize, Serialize,
    de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fmt,
    fs::File,
    io::{self, BufReader, BufWriter, Read, Write},
    path::Path,
    time::{Duration, Instant},
};

const TABLES: &[&str] = &[
    "vocabulary_lists",
    "words",
    "lexical_units",
    "word_senses",
    "sense_examples",
    "learning_units",
    "learning_unit_senses",
    "word_list_memberships",
    "word_list_learning_units",
    "word_list_learning_scopes",
    "word_forms",
    "lexical_relations",
    "word_frequencies",
];

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub table: String,
    pub key: Map<String, Value>,
    pub before: Option<Map<String, Value>>,
    pub after: Map<String, Value>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub delete: bool,
}
fn is_false(value: &bool) -> bool {
    !*value
}

fn shared(table: &str) -> &'static str {
    match table {
        "words" | "lexical_units" => "r.owner_id IS NULL",
        "word_senses"
        | "learning_units"
        | "word_list_memberships"
        | "word_list_learning_scopes" => {
            "EXISTS(SELECT 1 FROM words w WHERE w.id=r.word_id AND w.owner_id IS NULL)"
        }
        "sense_examples" | "learning_unit_senses" => {
            "EXISTS(SELECT 1 FROM word_senses s JOIN words w ON w.id=s.word_id WHERE s.id=r.sense_id AND w.owner_id IS NULL)"
        }
        "word_list_learning_units" => {
            "EXISTS(SELECT 1 FROM learning_units u JOIN words w ON w.id=u.word_id WHERE u.id=r.learning_unit_id AND w.owner_id IS NULL)"
        }
        "word_forms" => {
            "EXISTS(SELECT 1 FROM lexical_units u WHERE u.id=r.lexical_unit_id AND u.owner_id IS NULL)"
        }
        "lexical_relations" => {
            "EXISTS(SELECT 1 FROM lexical_units a JOIN lexical_units b ON b.id=r.to_unit_id WHERE a.id=r.from_unit_id AND a.owner_id IS NULL AND b.owner_id IS NULL)"
        }
        _ => "1",
    }
}
fn record_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Map<String, Value>> {
    use rusqlite::types::ValueRef;
    let mut result = Map::new();
    for (index, name) in row.as_ref().column_names().iter().enumerate() {
        let value = match row.get_ref(index)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(v) => serde_json::json!(v),
            ValueRef::Real(v) => serde_json::json!(v),
            ValueRef::Text(v) => serde_json::json!(String::from_utf8_lossy(v)),
            ValueRef::Blob(_) => return Err(rusqlite::Error::InvalidQuery),
        };
        result.insert((*name).into(), value);
    }
    Ok(result)
}

/// Produce a content-only patch from two server-local snapshots of the same database.
/// Both snapshots remain read-only; no account/history/session tables are traversed.
pub fn diff(before: &Path, after: &Path, output: &Path) -> Result<u64> {
    ensure!(!output.exists(), "output exists");
    let old = english_storage::open_readonly(before)?;
    let new = english_storage::open_readonly(after)?;
    old.execute_batch("BEGIN")?;
    new.execute_batch("BEGIN")?;
    let (identity, revision): (String, i64) = old.query_row(
        "SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        new.query_row(
            "SELECT database_id FROM meaning_catalog_revision WHERE id=1",
            [],
            |r| r.get::<_, String>(0)
        )? == identity,
        "snapshots must share stable dictionary IDs"
    );
    let mut temporary =
        tempfile::NamedTempFile::new_in(output.parent().context("output parent missing")?)?;
    let mut count = 0u64;
    let mut bytes = 0usize;
    {
        let mut writer = BufWriter::with_capacity(65536, &mut temporary);
        write!(
            writer,
            "{{\"format_version\":1,\"database_id\":{},\"base_revision\":{},\"records\":[",
            serde_json::to_string(&identity)?,
            revision
        )?;
        let mut emit = |record: Record| -> Result<()> {
            validate_record(&record)?;
            let data = serde_json::to_vec(&record)?;
            bytes += data.len();
            ensure!(
                bytes <= 500 * 1024 * 1024 && data.len() <= 2 * 1024 * 1024,
                "content diff exceeds budget"
            );
            if count > 0 {
                writer.write_all(b",")?;
            }
            writer.write_all(&data)?;
            count += 1;
            Ok(())
        };
        for &table in TABLES {
            let info = old
                .prepare(&format!("PRAGMA table_info({table})"))?
                .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let key_fields = info
                .iter()
                .filter(|(_, pk)| *pk > 0)
                .map(|(name, _)| name.clone())
                .collect::<Vec<_>>();
            ensure!(!key_fields.is_empty(), "content table lacks primary key");
            let clause = key_fields
                .iter()
                .map(|f| format!("{f} IS ?"))
                .collect::<Vec<_>>()
                .join(" AND ");
            let mut lookup = old.prepare(&format!("SELECT * FROM {table} WHERE {clause}"))?;
            let mut statement = new.prepare(&format!(
                "SELECT r.* FROM {table} r WHERE {} ORDER BY {}",
                shared(table),
                key_fields.join(",")
            ))?;
            let mut rows = statement.query([])?;
            while let Some(row) = rows.next()? {
                let after = record_row(row)?;
                let key = key_fields
                    .iter()
                    .map(|key| (key.clone(), after[key].clone()))
                    .collect::<Map<_, _>>();
                let key_values = key.values().map(sql_value).collect::<Result<Vec<_>>>()?;
                let before = lookup
                    .query_row(params_from_iter(key_values), record_row)
                    .optional()?;
                if let Some(before) = before {
                    let changed = after
                        .iter()
                        .filter(|(key, value)| before.get(*key) != Some(*value))
                        .map(|(k, v)| (k.clone(), v.clone()))
                        .collect::<Map<_, _>>();
                    if changed.is_empty() {
                        continue;
                    }
                    let expected = changed
                        .keys()
                        .map(|k| (k.clone(), before.get(k).cloned().unwrap_or(Value::Null)))
                        .collect();
                    emit(Record {
                        table: table.into(),
                        key,
                        before: Some(expected),
                        after: changed,
                        delete: false,
                    })?;
                } else {
                    emit(Record {
                        table: table.into(),
                        key,
                        before: None,
                        after,
                        delete: false,
                    })?;
                }
            }
            let mut statement = old.prepare(&format!(
                "SELECT r.* FROM {table} r WHERE {} ORDER BY {}",
                shared(table),
                key_fields.join(",")
            ))?;
            let mut rows = statement.query([])?;
            let mut exists = new.prepare(&format!("SELECT 1 FROM {table} WHERE {clause}"))?;
            while let Some(row) = rows.next()? {
                let before = record_row(row)?;
                let key = key_fields
                    .iter()
                    .map(|key| (key.clone(), before[key].clone()))
                    .collect::<Map<_, _>>();
                if exists
                    .query_row(
                        params_from_iter(key.values().map(sql_value).collect::<Result<Vec<_>>>()?),
                        |_| Ok(()),
                    )
                    .optional()?
                    .is_none()
                {
                    ensure!(
                        [
                            "word_list_memberships",
                            "word_list_learning_units",
                            "word_list_learning_scopes"
                        ]
                        .contains(&table),
                        "dictionary IDs must be archived, never deleted"
                    );
                    emit(Record {
                        table: table.into(),
                        key,
                        before: Some(before),
                        after: Map::new(),
                        delete: true,
                    })?;
                }
            }
        }
        writer.write_all(b"]}")?;
        writer.flush()?;
    }
    temporary.as_file().sync_all()?;
    temporary.persist_noclobber(output)?;
    Ok(count)
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format_version: u32,
    pub database_id: String,
    pub base_revision: i64,
    pub records: u64,
    pub sha256: String,
}

#[derive(Serialize)]
pub struct ApplyResult {
    pub applied: u64,
    pub already_applied: bool,
    pub dry_run: bool,
    pub sha256: String,
}

/// Limit before Serde allocates long tokens or a giant nested record.
struct Limited<R> {
    inner: R,
    total: u64,
    depth: usize,
    string: bool,
    escape: bool,
    token: usize,
    record: usize,
}
impl<R: Read> Read for Limited<R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let count = self.inner.read(output)?;
        self.total += count as u64;
        if self.total > 512 * 1024 * 1024 {
            return Err(io::Error::other("package exceeds 512 MiB"));
        }
        for &byte in &output[..count] {
            if self.depth >= 3 {
                self.record += 1;
            }
            if self.string {
                self.token += 1;
                if self.escape {
                    self.escape = false;
                } else if byte == b'\\' {
                    self.escape = true;
                } else if byte == b'"' {
                    self.string = false;
                }
            } else {
                match byte {
                    b'"' => {
                        self.string = true;
                        self.token = 0;
                    }
                    b'{' | b'[' => {
                        if self.depth == 2 {
                            self.record = 1;
                        }
                        self.depth += 1;
                    }
                    b'}' | b']' => {
                        self.depth = self.depth.saturating_sub(1);
                    }
                    _ => {}
                }
            }
            if self.depth > 32 || self.token > 2 * 1024 * 1024 || self.record > 2 * 1024 * 1024 {
                return Err(io::Error::other(
                    "content record/token/depth exceeds budget",
                ));
            }
        }
        Ok(count)
    }
}

struct PackageSeed<'a> {
    db: &'a Connection,
}
struct RecordsSeed<'a> {
    db: &'a Connection,
}
impl<'de> DeserializeSeed<'de> for RecordsSeed<'_> {
    type Value = u64;
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<u64, D::Error> {
        struct Items<'a>(&'a Connection);
        impl<'de> Visitor<'de> for Items<'_> {
            type Value = u64;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("content records")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<u64, A::Error> {
                let mut count = 0u64;
                while let Some(record) = seq.next_element::<Record>()? {
                    validate_record(&record).map_err(de::Error::custom)?;
                    let bytes = serde_json::to_vec(&record).map_err(de::Error::custom)?;
                    if bytes.len() > 2 * 1024 * 1024 {
                        return Err(de::Error::custom("record too large"));
                    }
                    self.0
                        .execute(
                            "INSERT INTO records(position,payload) VALUES(?,?)",
                            params![count, bytes],
                        )
                        .map_err(de::Error::custom)?;
                    count += 1;
                    if count > 1_000_000 {
                        return Err(de::Error::custom("too many records"));
                    }
                }
                Ok(count)
            }
        }
        deserializer.deserialize_seq(Items(self.db))
    }
}

impl<'de> DeserializeSeed<'de> for PackageSeed<'_> {
    type Value = Manifest;
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<Manifest, D::Error> {
        struct Document<'a>(&'a Connection);
        impl<'de> Visitor<'de> for Document<'_> {
            type Value = Manifest;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("versioned content package")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Manifest, A::Error> {
                let mut seen = BTreeSet::new();
                let (mut version, mut identity, mut revision, mut records) =
                    (None, None, None, None);
                while let Some(key) = map.next_key::<String>()? {
                    if !seen.insert(key.clone()) {
                        return Err(de::Error::custom("duplicate package field"));
                    }
                    match key.as_str() {
                        "format_version" => version = Some(map.next_value::<u32>()?),
                        "database_id" => identity = Some(map.next_value::<String>()?),
                        "base_revision" => revision = Some(map.next_value::<i64>()?),
                        "records" => {
                            records = Some(map.next_value_seed(RecordsSeed { db: self.0 })?)
                        }
                        _ => return Err(de::Error::custom("unknown package field")),
                    }
                }
                let manifest = Manifest {
                    format_version: version
                        .ok_or_else(|| de::Error::missing_field("format_version"))?,
                    database_id: identity.ok_or_else(|| de::Error::missing_field("database_id"))?,
                    base_revision: revision
                        .ok_or_else(|| de::Error::missing_field("base_revision"))?,
                    records: records.ok_or_else(|| de::Error::missing_field("records"))?,
                    sha256: String::new(),
                };
                if manifest.format_version != 1
                    || manifest.database_id.len() != 32
                    || !manifest.database_id.bytes().all(|b| b.is_ascii_hexdigit())
                    || manifest.base_revision < 0
                {
                    return Err(de::Error::custom(
                        "unsupported content package identity/version",
                    ));
                }
                Ok(manifest)
            }
        }
        deserializer.deserialize_map(Document(self.db))
    }
}

fn validate_record(record: &Record) -> Result<()> {
    ensure!(
        TABLES.contains(&record.table.as_str()),
        "unsupported content table"
    );
    ensure!(
        !record.key.is_empty() && record.key.len() <= 3,
        "invalid content key"
    );
    ensure!(
        (record.delete || !record.after.is_empty()) && record.after.len() <= 32,
        "invalid after fields"
    );
    if record.delete {
        ensure!(
            record.before.is_some()
                && record.after.is_empty()
                && [
                    "word_list_memberships",
                    "word_list_learning_units",
                    "word_list_learning_scopes"
                ]
                .contains(&record.table.as_str()),
            "only list mappings may be removed"
        );
    }
    for (key, value) in record
        .key
        .iter()
        .chain(record.after.iter())
        .chain(record.before.iter().flat_map(|r| r.iter()))
    {
        ensure!(
            !key.is_empty() && key.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'),
            "invalid column"
        );
        ensure!(
            value.is_null() || value.is_string() || value.is_i64() || value.is_f64(),
            "invalid SQLite value"
        );
        ensure!(
            value.as_str().is_none_or(|v| v.len() <= 1024 * 1024),
            "content string too large"
        );
    }
    for (key, value) in &record.key {
        ensure!(!value.is_null(), "null key");
        ensure!(
            !record.after.contains_key(key) || record.after[key] == *value,
            "cannot change key"
        );
    }
    if let Some(before) = &record.before {
        ensure!(
            record
                .after
                .keys()
                .all(|key| before.contains_key(key) || record.key.contains_key(key)),
            "update requires previous field values"
        );
    }
    Ok(())
}

fn package_hash(conn: &Connection, manifest: &Manifest) -> Result<String> {
    let mut hash = Sha256::new();
    hash.update(serde_json::to_vec(&(
        manifest.format_version,
        &manifest.database_id,
        manifest.base_revision,
    ))?);
    let mut statement = conn.prepare("SELECT payload FROM records ORDER BY position")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        hash.update([0]);
        hash.update(row.get::<_, Vec<u8>>(0)?);
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub fn build(source: &Path, output: &Path) -> Result<Manifest> {
    ensure!(!output.exists(), "output exists");
    let parent = output.parent().context("output parent missing")?;
    let temp = tempfile::NamedTempFile::new_in(parent)?;
    let conn = Connection::open(temp.path())?;
    conn.execute_batch("PRAGMA cache_size=-2048; PRAGMA temp_store=FILE; BEGIN; CREATE TABLE records(position INTEGER PRIMARY KEY,payload BLOB NOT NULL); CREATE TABLE manifest(value TEXT NOT NULL);")?;
    let input = Limited {
        inner: BufReader::with_capacity(65536, File::open(source)?),
        total: 0,
        depth: 0,
        string: false,
        escape: false,
        token: 0,
        record: 0,
    };
    let mut parser = serde_json::Deserializer::from_reader(input);
    let mut manifest = PackageSeed { db: &conn }.deserialize(&mut parser)?;
    parser.end()?;
    manifest.sha256 = package_hash(&conn, &manifest)?;
    conn.execute(
        "INSERT INTO manifest VALUES(?)",
        [serde_json::to_string(&manifest)?],
    )?;
    conn.execute_batch("COMMIT")?;
    drop(conn);
    temp.as_file().sync_all()?;
    temp.persist_noclobber(output)?;
    Ok(manifest)
}

pub fn validate(package: &Path) -> Result<Manifest> {
    let conn = Connection::open_with_flags(package, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    validate_connection(&conn)
}

fn validate_connection(conn: &Connection) -> Result<Manifest> {
    conn.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        2 * 1024 * 1024,
    )?;
    conn.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-2048;")?;
    ensure!(
        conn.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))? == "ok",
        "corrupt package"
    );
    let encoded: String = conn.query_row("SELECT value FROM manifest", [], |r| r.get(0))?;
    let manifest: Manifest = serde_json::from_str(&encoded)?;
    ensure!(manifest.format_version == 1, "unknown package version");
    ensure!(
        manifest.sha256 == package_hash(conn, &manifest)?,
        "package fingerprint mismatch"
    );
    let mut count = 0;
    let mut statement = conn.prepare("SELECT payload FROM records ORDER BY position")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        validate_record(&serde_json::from_slice::<Record>(
            &row.get::<_, Vec<u8>>(0)?,
        )?)?;
        count += 1;
    }
    ensure!(count == manifest.records, "package count mismatch");
    Ok(manifest)
}

fn sql_value(value: &Value) -> Result<SqlValue> {
    Ok(match value {
        Value::Null => SqlValue::Null,
        Value::String(s) => SqlValue::Text(s.clone()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                SqlValue::Integer(i)
            } else {
                SqlValue::Real(n.as_f64().context("invalid number")?)
            }
        }
        _ => bail!("invalid SQLite value"),
    })
}

fn check_scope(conn: &Connection, record: &Record) -> Result<()> {
    let id = record.key.get("id").and_then(Value::as_i64);
    let word = record
        .after
        .get("word_id")
        .or_else(|| record.key.get("word_id"))
        .and_then(Value::as_i64);
    if record.table == "words" {
        ensure!(
            record.after.get("owner_id").is_none_or(Value::is_null),
            "private word ownership cannot be changed"
        );
        if let Some(id) = id {
            ensure!(
                !conn.query_row(
                    "SELECT EXISTS(SELECT 1 FROM words WHERE id=? AND owner_id IS NOT NULL)",
                    [id],
                    |r| r.get::<_, bool>(0)
                )?,
                "private words are excluded"
            );
        }
    }
    if let Some(word) = word {
        ensure!(
            conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM words WHERE id=? AND owner_id IS NULL)",
                [word],
                |r| r.get::<_, bool>(0)
            )?,
            "content references a private/missing word"
        );
    }
    // Existing indirect rows may not expose word_id in their changed fields.
    let scope_query = match record.table.as_str() {
        "word_forms" => Some(
            "SELECT u.owner_id FROM word_forms f JOIN lexical_units u ON u.id=f.lexical_unit_id WHERE f.id=?",
        ),
        "lexical_relations" => Some(
            "SELECT COALESCE(a.owner_id,b.owner_id) FROM lexical_relations e JOIN lexical_units a ON a.id=e.from_unit_id JOIN lexical_units b ON b.id=e.to_unit_id WHERE e.id=?",
        ),
        "word_senses" => {
            Some("SELECT w.owner_id FROM word_senses s JOIN words w ON w.id=s.word_id WHERE s.id=?")
        }
        "sense_examples" => Some(
            "SELECT w.owner_id FROM sense_examples e JOIN word_senses s ON s.id=e.sense_id JOIN words w ON w.id=s.word_id WHERE e.id=?",
        ),
        "learning_units" => Some(
            "SELECT w.owner_id FROM learning_units u JOIN words w ON w.id=u.word_id WHERE u.id=?",
        ),
        "lexical_units" => Some("SELECT owner_id FROM lexical_units WHERE id=?"),
        _ => None,
    };
    if let (Some(query), Some(id)) = (scope_query, id) {
        let owner: Option<Option<i64>> = conn.query_row(query, [id], |r| r.get(0)).optional()?;
        ensure!(owner.flatten().is_none(), "private content row is excluded");
    }
    ensure!(
        record.after.get("owner_id").is_none_or(Value::is_null),
        "private content is excluded"
    );
    for (field, query) in [
        (
            "sense_id",
            "SELECT w.owner_id FROM word_senses s JOIN words w ON w.id=s.word_id WHERE s.id=?",
        ),
        (
            "anchor_sense_id",
            "SELECT w.owner_id FROM word_senses s JOIN words w ON w.id=s.word_id WHERE s.id=?",
        ),
        (
            "learning_unit_id",
            "SELECT w.owner_id FROM learning_units u JOIN words w ON w.id=u.word_id WHERE u.id=?",
        ),
        (
            "lexical_unit_id",
            "SELECT owner_id FROM lexical_units WHERE id=?",
        ),
        (
            "from_unit_id",
            "SELECT owner_id FROM lexical_units WHERE id=?",
        ),
        (
            "to_unit_id",
            "SELECT owner_id FROM lexical_units WHERE id=?",
        ),
    ] {
        if let Some(id) = record
            .after
            .get(field)
            .or_else(|| record.key.get(field))
            .and_then(Value::as_i64)
        {
            let owner: Option<Option<i64>> =
                conn.query_row(query, [id], |r| r.get(0)).optional()?;
            ensure!(
                matches!(owner, Some(None)),
                "content reference is private or missing"
            );
        }
    }
    Ok(())
}

pub fn apply(package: &Path, database: &Path, dry_run: bool) -> Result<ApplyResult> {
    let input = Connection::open_with_flags(package, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    input.execute_batch("BEGIN")?;
    let manifest = validate_connection(&input)?; // Keep the same read snapshot for validation and apply.
    let mut conn =
        Connection::open_with_flags(database, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    conn.busy_timeout(Duration::from_secs(1))?;
    conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA cache_size=-2048;")?;
    ensure!(
        conn.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? == 22,
        "unsupported database schema"
    );
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let receipt = format!("rust_content:{}", manifest.sha256);
    if tx
        .query_row(
            "SELECT value FROM vocabulary_catalog_state WHERE key=?",
            [&receipt],
            |r| r.get::<_, String>(0),
        )
        .optional()?
        .is_some()
    {
        return Ok(ApplyResult {
            applied: 0,
            already_applied: true,
            dry_run,
            sha256: manifest.sha256,
        });
    }
    let (identity, revision): (String, i64) = tx.query_row(
        "SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        manifest.database_id == identity && manifest.base_revision == revision,
        "content base revision changed"
    );
    let deadline = Instant::now() + Duration::from_secs(120);
    let mut statement = input.prepare("SELECT payload FROM records ORDER BY position")?;
    let mut rows = statement.query([])?;
    let mut changed = 0;
    while let Some(row) = rows.next()? {
        ensure!(Instant::now() < deadline, "content apply deadline exceeded");
        let record: Record = serde_json::from_slice(&row.get::<_, Vec<u8>>(0)?)?;
        let mut info = tx.prepare(&format!("PRAGMA table_info({})", record.table))?;
        let columns = info
            .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(5)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let primary: BTreeSet<_> = columns
            .iter()
            .filter(|(_, pk)| *pk > 0)
            .map(|(name, _)| name.clone())
            .collect();
        ensure!(
            record.key.keys().cloned().collect::<BTreeSet<_>>() == primary,
            "key must match primary key"
        );
        for field in record
            .after
            .keys()
            .chain(record.before.iter().flat_map(|r| r.keys()))
        {
            ensure!(
                columns.iter().any(|(name, _)| name == field),
                "unknown content column"
            );
        }
        check_scope(&tx, &record)?;
        let key_clause = record
            .key
            .keys()
            .map(|k| format!("{k} IS ?"))
            .collect::<Vec<_>>()
            .join(" AND ");
        let key_values = record
            .key
            .values()
            .map(sql_value)
            .collect::<Result<Vec<_>>>()?;
        let exists: bool = tx.query_row(
            &format!(
                "SELECT EXISTS(SELECT 1 FROM {} WHERE {})",
                record.table, key_clause
            ),
            params_from_iter(&key_values),
            |r| r.get(0),
        )?;
        match &record.before {
            None => {
                ensure!(!exists, "insert key already exists");
                let mut data = record.after.clone();
                for (key, value) in &record.key {
                    data.insert(key.clone(), value.clone());
                }
                let fields = data.keys().cloned().collect::<Vec<_>>().join(",");
                let placeholders = vec!["?"; data.len()].join(",");
                let values = data.values().map(sql_value).collect::<Result<Vec<_>>>()?;
                tx.execute(
                    &format!(
                        "INSERT INTO {} ({fields}) VALUES({placeholders})",
                        record.table
                    ),
                    params_from_iter(values),
                )?;
                changed += 1;
            }
            Some(before) => {
                for field in [
                    "word_id",
                    "sense_id",
                    "learning_unit_id",
                    "lexical_unit_id",
                    "representative_sense_id",
                    "from_unit_id",
                    "to_unit_id",
                    "owner_id",
                ] {
                    if let Some(after) = record.after.get(field) {
                        ensure!(
                            before.get(field) == Some(after)
                                || record.key.get(field) == Some(after),
                            "structural rebind requires a dedicated migration"
                        );
                    }
                }
                ensure!(exists, "update key missing");
                let mut expected_values = key_values.clone();
                expected_values.extend(before.values().map(sql_value).collect::<Result<Vec<_>>>()?);
                let compare = before
                    .keys()
                    .map(|k| format!("{k} IS ?"))
                    .collect::<Vec<_>>()
                    .join(" AND ");
                let matches: bool = tx.query_row(
                    &format!(
                        "SELECT EXISTS(SELECT 1 FROM {} WHERE {}{})",
                        record.table,
                        key_clause,
                        if compare.is_empty() {
                            String::new()
                        } else {
                            format!(" AND {compare}")
                        }
                    ),
                    params_from_iter(expected_values),
                    |r| r.get(0),
                )?;
                ensure!(matches, "content expected values changed");
                if record.delete {
                    tx.execute(
                        &format!("DELETE FROM {} WHERE {}", record.table, key_clause),
                        params_from_iter(key_values),
                    )?;
                    changed += 1;
                    continue;
                }
                if record
                    .after
                    .iter()
                    .all(|(key, value)| before.get(key) == Some(value))
                {
                    continue;
                }
                let fields = record
                    .after
                    .keys()
                    .map(|k| format!("{k}=?"))
                    .collect::<Vec<_>>()
                    .join(",");
                let mut values = record
                    .after
                    .values()
                    .map(sql_value)
                    .collect::<Result<Vec<_>>>()?;
                values.extend(key_values);
                tx.execute(
                    &format!("UPDATE {} SET {fields} WHERE {key_clause}", record.table),
                    params_from_iter(values),
                )?;
                changed += 1;
            }
        }
    }
    tx.execute("INSERT OR IGNORE INTO user_vocabulary_lists(user_id,list_id,selected) SELECT u.id,v.list_id,v.default_selected FROM users u CROSS JOIN vocabulary_lists v WHERE u.role='learner'",[])?;
    ensure!(
        tx.query_row("PRAGMA foreign_key_check", [], |_| Ok(()))
            .optional()?
            .is_none(),
        "content foreign key violation"
    );
    tx.execute(
        "INSERT INTO vocabulary_catalog_state(key,value) VALUES(?,?)",
        params![receipt, serde_json::to_string(&manifest)?],
    )?;
    if dry_run {
        tx.rollback()?;
    } else {
        tx.commit()?;
    }
    Ok(ApplyResult {
        applied: changed,
        already_applied: false,
        dry_run,
        sha256: manifest.sha256,
    })
}
