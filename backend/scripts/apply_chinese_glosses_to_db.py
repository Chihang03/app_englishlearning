#!/usr/bin/env python3
"""Apply only missing public Chinese glosses, with a backup and progress checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def catalog_structure(document: dict) -> str:
    # Chinese glosses are the only catalog fields this release may change.
    for word in document["words"]:
        word.pop("definition_cn", None)
        for sense in word["senses"]:
            sense.pop("definition_cn", None)
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def unchanged_content(conn: sqlite3.Connection) -> dict[str, str]:
    hashes = {}
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")]
        if name in ("words", "word_senses"):
            columns.remove("definition_cn")
        projection = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        where = " WHERE key <> 'fingerprint'" if name == "vocabulary_catalog_state" else ""
        digest = hashlib.sha256()
        for row in conn.execute(f"SELECT {projection} FROM {quoted}{where} ORDER BY rowid"):
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        hashes[name] = digest.hexdigest()
    return hashes


def apply(db: Path, catalog: Path, previous_catalog: Path, supplements: Path, seed: Path, backup: Path) -> dict:
    if not db.is_file() or backup.exists():
        raise ValueError("Database must exist and backup must be a new file")
    previous_bytes, new_bytes, seed_bytes = previous_catalog.read_bytes(), catalog.read_bytes(), seed.read_bytes()
    before_catalog = json.loads(previous_bytes)
    after_catalog = json.loads(new_bytes)
    for old, new in zip(before_catalog["words"], after_catalog["words"]):
        if old.get("definition_cn") and old["definition_cn"] != new.get("definition_cn"):
            raise ValueError(f"Existing Chinese word gloss changed: {old['word']}")
        for old_sense, new_sense in zip(old["senses"], new["senses"]):
            if old_sense.get("definition_cn") and old_sense["definition_cn"] != new_sense.get("definition_cn"):
                raise ValueError(f"Existing Chinese sense gloss changed: {old['word']}")
    if catalog_structure(before_catalog) != catalog_structure(json.loads(new_bytes)):
        raise ValueError("Catalog changes include fields other than Chinese definitions")
    entries = json.loads(supplements.read_text(encoding="utf-8"))["entries"]
    new_by_key = {(w["word"], s["key"]): s for w in after_catalog["words"] for s in w["senses"]}
    for entry in entries:
        sense = new_by_key[(entry["word"], entry["sense_key"])]
        if (sense["definition_cn"] != entry["definition_cn"] or sense["definition_en"] != entry["expected_definition_en"]
                or sense["part_of_speech"] != entry["expected_part_of_speech"]):
            raise ValueError(f"Supplement/catalog mismatch: {entry['word']}")
    conn = sqlite3.connect(db, timeout=15)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with sqlite3.connect(backup) as destination:
            conn.backup(destination)
        conn.execute("BEGIN IMMEDIATE")
        old_fingerprint = hashlib.sha256(previous_bytes + b"\0" + seed_bytes).hexdigest()
        state = conn.execute("SELECT value FROM vocabulary_catalog_state WHERE key='fingerprint'").fetchone()
        if not state or state[0] != old_fingerprint:
            raise ValueError("Live catalog fingerprint differs; inspect before applying")
        preserved = unchanged_content(conn)
        changed = 0
        already_present = 0
        for entry in entries:
            row = conn.execute("""SELECT s.id,s.definition_cn,s.definition_en,s.part_of_speech
                FROM word_senses s JOIN words w ON w.id=s.word_id
                WHERE w.owner_id IS NULL AND w.word=? AND s.sense_key=? AND s.active=1""",
                (entry["word"], entry["sense_key"])).fetchone()
            if not row or row[2] != entry["expected_definition_en"] or row[3] != entry["expected_part_of_speech"]:
                raise ValueError(f"Live sense differs: {entry['word']} {entry['sense_key']}")
            if str(row[1] or "").strip():
                already_present += 1
                continue
            conn.execute("UPDATE word_senses SET definition_cn=? WHERE id=?", (entry["definition_cn"], row[0]))
            changed += 1
        changed_words = 0
        for word in after_catalog["words"]:
            if not word["definition_cn"]:
                continue
            changed_words += conn.execute("""UPDATE words SET definition_cn=?
                WHERE owner_id IS NULL AND word=? AND COALESCE(definition_en,'')=COALESCE(?,'')
                AND (trim(definition_cn)='' OR definition_cn='词义待补充')""",
                (word["definition_cn"], word["word"], word.get("definition_en"))).rowcount
        # Prevent a full startup import from merging newly matching starter senses.
        # This narrowly updates the importer fingerprint; IDs/examples stay intact.
        conn.execute("UPDATE vocabulary_catalog_state SET value=? WHERE key='fingerprint'",
            (hashlib.sha256(new_bytes + b"\0" + seed_bytes).hexdigest(),))
        if unchanged_content(conn) != preserved:
            raise ValueError("Unexpected change outside Chinese definitions/fingerprint")
        if conn.execute("PRAGMA foreign_key_check").fetchall() or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        remaining = conn.execute("""SELECT count(*) FROM word_senses s JOIN words w ON w.id=s.word_id
            WHERE w.owner_id IS NULL AND s.active=1 AND trim(s.definition_cn)=''""").fetchone()[0]
        conn.commit()
        return {"updated_senses": changed, "updated_word_glosses": changed_words, "already_present": already_present,
            "remaining_public_senses_without_chinese": remaining, "other_content_and_learning_progress_unchanged": True,
            "backup": str(backup)}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("db", "catalog", "previous-catalog", "supplements", "seed", "backup"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(apply(args.db, args.catalog, args.previous_catalog, args.supplements, args.seed, args.backup), ensure_ascii=False))


if __name__ == "__main__":
    main()
