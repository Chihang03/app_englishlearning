"""Read-only, account-visible dictionary snapshot for durable client caching."""
from __future__ import annotations

import hashlib
import json
import fcntl
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


logger = logging.getLogger(__name__)


def _native_catalog(conn, user_id: int, identity, target: Path) -> str | None:
    """Optional bounded Rust exporter. The caller holds the cache lock/snapshot.

    A revision mismatch or native failure uses the existing Python snapshot,
    so a content edit racing the child cannot publish mismatched ETag metadata.
    """
    executable = os.environ.get("ENGLISH_NATIVE_CATALOG", "")
    if not executable:
        return None
    try:
        if not Path(executable).is_absolute():
            raise ValueError("native exporter must use an absolute path")
        database_path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not database_path:
            return None
        with tempfile.TemporaryDirectory(prefix=".native-", dir=target.parent) as directory:
            output = Path(directory) / "catalog.json"
            completed = subprocess.run(
                [executable, "catalog", "--database", database_path,
                 "--user-id", str(int(user_id)), "--database-id", str(identity[0]),
                 "--revision", str(identity[1]), "--output", str(output), "--timeout-seconds", "30"],
                check=True, capture_output=True, text=True, timeout=35,
            )
            if len(completed.stdout) > 4096:
                raise ValueError("oversized native metadata")
            result = json.loads(completed.stdout)
            if (not isinstance(result, dict) or result.get("database_id") != identity[0] or result.get("revision") != identity[1]
                    or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("sha256", "")))
                    or output.stat().st_size != result.get("bytes")):
                raise ValueError("invalid native metadata")
            digest = hashlib.sha256()
            with output.open("rb") as source:
                while chunk := source.read(65536):
                    digest.update(chunk)
            if digest.hexdigest() != result["sha256"]:
                raise ValueError("native output digest mismatch")
            os.replace(output, target)
            return '"' + result["sha256"] + '"'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # No native stdout/stderr or user content is included in logs.
        logger.warning("Native catalogue unavailable; using Python snapshot exporter")
        return None


def catalog_words(conn, user_id: int) -> Iterator[dict]:
    rows = conn.execute("""
        SELECT w.id AS word_id, w.word, s.id AS sense_id, b.learning_unit_id,
               s.part_of_speech, s.definition_cn, s.definition_en,
               CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN u.display_definition_cn END AS core_definition_cn,
               CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN u.display_definition_en END AS core_definition_en,
               CASE WHEN u.unit_key LIKE 'semantic:%' AND COALESCE(json_extract(u.provenance_json,'$.review_pending'),0)=0 THEN json_extract(u.provenance_json,'$.notes') END AS semantic_notes,
               e.id AS example_id, e.sentence, e.translation_cn, e.target_form
        FROM words w
        JOIN word_senses s ON s.word_id=w.id AND s.active=1
        JOIN learning_unit_senses b ON b.sense_id=s.id
        JOIN learning_units u ON u.id=b.learning_unit_id
        LEFT JOIN sense_examples e ON e.sense_id=s.id AND e.active=1
        WHERE w.owner_id IS NULL OR w.owner_id=?
        ORDER BY w.id, s.position, s.id, e.id
    """, (user_id,))
    word: dict | None = None
    previous_word = previous_sense = None
    senses: list[dict] = []
    examples: list[dict] = []
    for row in rows:
        if row["word_id"] != previous_word:
            if word is not None:
                yield word
            word = {"id": row["word_id"], "word": row["word"], "senses": []}
            senses = word["senses"]
            previous_word = row["word_id"]
            previous_sense = None
        if row["sense_id"] != previous_sense:
            senses.append({
                "id": row["sense_id"], "learning_unit_id": row["learning_unit_id"],
                "part_of_speech": row["part_of_speech"],
                "definition_cn": row["definition_cn"], "definition_en": row["definition_en"],
                "core_definition_cn": row["core_definition_cn"], "core_definition_en": row["core_definition_en"],
                "semantic_notes": row["semantic_notes"],
                "examples": [],
            })
            examples = senses[-1]["examples"]
            previous_sense = row["sense_id"]
        if row["example_id"] is not None:
            examples.append({
                "id": row["example_id"], "sentence": row["sentence"],
                "translation_cn": row["translation_cn"], "target_form": row["target_form"],
            })
    if word is not None:
        yield word


@dataclass(frozen=True)
class CatalogFile:
    path: Path
    etag: str


def catalog_snapshot(conn, user_id: int, cache_dir: Path) -> CatalogFile:
    """Build once per content revision; serialize one word at a time to disk."""
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # flock also serializes builders in different workers. Readers use immutable
    # revision-specific files, so an update cannot mismatch a response's ETag.
    with (cache_dir / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn.execute("BEGIN")
        try:
            row = conn.execute("SELECT database_id,revision FROM meaning_catalog_revision WHERE id=1").fetchone()
            stem = f"v1-{row[0]}-{int(user_id)}-{row[1]}"
            target = cache_dir / (stem + ".json")
            metadata = cache_dir / (stem + ".etag")
            if target.is_file() and metadata.is_file():
                return CatalogFile(target, metadata.read_text(encoding="ascii"))
            etag = _native_catalog(conn, user_id, row, target)
            digest = hashlib.sha256()
            temporary = None
            try:
                if etag is None:
                    with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as output:
                        temporary = Path(output.name)
                        def write(chunk: bytes) -> None:
                            output.write(chunk)
                            digest.update(chunk)
                        write(b'{"words":[')
                        for index, word in enumerate(catalog_words(conn, user_id)):
                            if index:
                                write(b",")
                            write(json.dumps(word, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                        write(b"]}")
                    etag = '"' + digest.hexdigest() + '"'
                    os.replace(temporary, target)
                metadata.write_text(etag, encoding="ascii")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            # Keep recent revisions for responses already in flight.
            cutoff = time.time() - 86400
            for old in cache_dir.glob("v1-*"):
                if old not in (target, metadata) and old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            return CatalogFile(target, etag)
        finally:
            conn.rollback()
