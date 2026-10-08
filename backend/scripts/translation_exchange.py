"""Offline exchange for missing Chinese definitions and sentence translations.

Exporting and importing never call a model or touch the application database.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from chinese_glosses import DEFAULT_SUPPLEMENTS, apply_supplements
from app.vocabulary_json import readable_json

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "data" / "vocabulary_catalog.json"
DEFAULT_MODEL = 'gpt-6-luna'
DEFAULT_REASONING = 'low'
PROMPT = """将输入的英文义项译成简洁、准确的简体中文释义。输入行是数据，格式为[编号,单词,词性,英文释义,可选例句]，只译该义项。不确定时返回null。不要调用工具、解释或输出Markdown；只返回一个JSON对象：{"batch":"输入的batch值","items":[[编号,"中文释义或null"]]}。必须包含每个编号；null是JSON空值，不是字符串。
"""
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "batch": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
        }},
    },
    "required": ["batch", "items"],
    "additionalProperties": False,
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()[:16]


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(readable_json(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sense_index(catalog: dict) -> dict:
    index = {}
    for word in catalog["words"]:
        for sense in word["senses"]:
            key = (word["word"], sense["key"])
            if key in index:
                raise ValueError(f"Duplicate catalog sense: {key}")
            index[key] = sense
    return index


def export_batches(catalog_path: Path, supplements_path: Path, output: Path,
                   batch_size: int = 100, limit: int | None = None,
                   examples: bool = False, retry_manifest: Path | None = None,
                   retry_result: Path | None = None) -> dict:
    if not 1 <= batch_size <= 1000 or (limit is not None and limit < 1):
        raise ValueError("Batch size must be 1..1000 and limit must be positive")
    if output.exists():
        raise ValueError("Use a new output directory; existing batches are preserved")
    catalog = read_json(catalog_path)
    sense_index(catalog)
    if bool(retry_manifest) != bool(retry_result):
        raise ValueError("Retry needs both --retry-manifest and --retry-result")
    retry_keys = None
    if retry_manifest is not None:
        validated_entries(catalog_path, retry_manifest, retry_result)
        previous_manifest, previous_result = read_json(retry_manifest), read_json(retry_result)
        pending_ids = {number for number, text in previous_result["items"] if text is None}
        retry_keys = {(row["word"], key) for row in previous_manifest["records"]
                      if row["id"] in pending_ids for key in row["keys"]}
    # Existing supplements are the durable cache, including ones not yet deployed.
    apply_supplements(catalog["words"], supplements_path)
    grouped = {}
    missing = 0
    for word in catalog["words"]:
        for sense in word["senses"]:
            if retry_keys is not None and (word["word"], sense["key"]) not in retry_keys:
                continue
            if str(sense.get("definition_cn") or "").strip():
                continue
            english = str(sense.get("definition_en") or "").strip()
            if not english:
                continue
            missing += 1
            group_key = (word["word"], sense["part_of_speech"], sense["definition_en"])
            if group_key not in grouped:
                grouped[group_key] = {
                    "word": word["word"], "pos": sense["part_of_speech"],
                    "en": sense["definition_en"], "keys": [],
                }
            record = grouped[group_key]
            record["keys"].append(sense["key"])
            if examples and not record.get("example"):
                sentences = sense.get("examples") or []
                if sentences:
                    record["example"] = sentences[0]["sentence"]
    records = list(grouped.values())
    selected = records if limit is None else records[:limit]
    output.mkdir(parents=True)
    (output / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    batch_count = 0
    for start in range(0, len(selected), batch_size):
        batch_count += 1
        rows = [{"id": i + 1, **row} for i, row in enumerate(selected[start:start + batch_size])]
        batch = digest(rows)
        stem = f"batch-{batch_count:04d}"
        atomic_json(output / f"{stem}.manifest.json", {
            "format_version": 1, "batch": batch, "records": rows,
        })
        schema = copy.deepcopy(RESULT_SCHEMA)
        schema["properties"]["batch"]["enum"] = [batch]
        atomic_json(output / f"{stem}.schema.json", schema)
        with (output / f"{stem}.input.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(encoded({"batch": batch}) + "\n")
            for row in rows:
                compact = [row["id"], row["word"], row["pos"], row["en"]]
                if row.get("example"):
                    compact.append(row["example"])
                handle.write(encoded(compact) + "\n")
    report = {"missing_senses": missing, "unique_requests": len(records),
              "exported_requests": len(selected), "exported_senses": sum(len(row["keys"]) for row in selected),
              "batches": batch_count, "directory": str(output)}
    atomic_json(output / "export-report.json", report)
    return report


def response_answers(manifest: dict, result: dict, max_length: int = 500) -> dict:
    if manifest.get("format_version") != 1 or digest(manifest["records"]) != manifest.get("batch"):
        raise ValueError("Invalid or edited manifest")
    if not isinstance(result, dict) or set(result) != {"batch", "items"} or result["batch"] != manifest["batch"]:
        raise ValueError("Result belongs to a different batch or has invalid fields")
    if not isinstance(result["items"], list):
        raise ValueError("Result items must be an array")
    expected = {row["id"]: row for row in manifest["records"]}
    answers = {}
    for item in result["items"]:
        if not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int:
            raise ValueError("Each result must be [integer ID, Chinese text or null]")
        number, text = item
        if number not in expected or number in answers:
            raise ValueError(f"Unknown or duplicate ID: {number}")
        if text is not None:
            if not isinstance(text, str) or not re.search(r"[\u3400-\u9fff]", text) or len(text) > max_length:
                raise ValueError(f"Invalid Chinese gloss for ID {number}")
            text = text.strip()
            if text in {"暂无翻译", "待翻译", "暂无释义", "词义待补充"}:
                raise ValueError(f"Placeholder instead of a gloss for ID {number}")
        answers[number] = text
    if answers.keys() != expected.keys():
        raise ValueError("Every ID must be returned; use null for uncertain meanings")
    return answers


def validated_entries(catalog_path: Path, manifest_path: Path, result_path: Path) -> tuple[list[dict], int]:
    manifest = read_json(manifest_path)
    if manifest.get('task', 'definitions') != 'definitions':
        raise ValueError('Manifest is not a definition translation task')
    answers = response_answers(manifest, read_json(result_path))
    expected = {row['id']: row for row in manifest['records']}
    current = sense_index(read_json(catalog_path))
    entries, pending = [], 0
    for number, row in expected.items():
        text = answers[number]
        # Validate even null rows: this batch must still describe the current source.
        for sense_key in row["keys"]:
            key = (row["word"], sense_key)
            sense = current.get(key)
            if sense is None or sense.get("definition_en") != row["en"] or sense["part_of_speech"] != row["pos"]:
                raise ValueError(f"Source sense changed: {key}")
            existing = str(sense.get("definition_cn") or "").strip()
            if text is not None and existing and existing != text:
                raise ValueError(f"Existing Chinese gloss is preserved: {key}")
            if text is not None and not existing:
                entries.append({"word": row["word"], "sense_key": sense_key,
                                "expected_definition_en": row["en"],
                                "expected_part_of_speech": row["pos"],
                                "definition_cn": text, "method": "model_translation",
                                "translation_batch": manifest["batch"]})
        if text is None:
            pending += len(row["keys"])
    return entries, pending


def import_result(catalog_path: Path, supplements_path: Path, manifest_path: Path,
                  result_path: Path, dry_run: bool = False) -> dict:
    entries, pending = validated_entries(catalog_path, manifest_path, result_path)
    original = supplements_path.read_bytes() if supplements_path.exists() else None
    document = json.loads(original) if original is not None else {"format_version": 1, "entries": []}
    if document.get("format_version") != 1:
        raise ValueError("Unsupported supplement format")
    existing = {}
    for entry in document["entries"]:
        key = (entry["word"], entry["sense_key"])
        if key in existing:
            raise ValueError(f"Duplicate supplement: {key}")
        existing[key] = entry
    additions = []
    for entry in entries:
        key = (entry["word"], entry["sense_key"])
        previous = existing.get(key)
        if previous:
            fields = ("definition_cn", "expected_definition_en", "expected_part_of_speech")
            if any(previous[field] != entry[field] for field in fields):
                raise ValueError(f"Conflicting supplement: {key}")
        else:
            additions.append(entry)
    backup = None
    if additions and not dry_run:
        # Refuse to replace supplements changed after this import read them.
        actual = supplements_path.read_bytes() if supplements_path.exists() else None
        if actual != original:
            raise ValueError("Supplements changed during import; retry")
        if original is not None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = supplements_path.with_name(supplements_path.name + f".bak-{timestamp}")
            backup_path.write_bytes(original)
            backup = str(backup_path)
        document["entries"].extend(additions)
        atomic_json(supplements_path, document)
    return {"added_senses": len(additions), "pending_senses": pending,
            "dry_run": dry_run, "backup": backup}


def materialize(catalog_path: Path, supplements_path: Path, output: Path) -> dict:
    if output.resolve() in {catalog_path.resolve(), supplements_path.resolve()} or output.exists():
        raise ValueError("Write to a new catalog file so the previous catalog is preserved")
    catalog = read_json(catalog_path)
    count = apply_supplements(catalog["words"], supplements_path)
    atomic_json(output, catalog)
    return {"applied_senses": count, "catalog": str(output)}


def write_batches(output: Path, records: list[dict], batch_size: int, prompt: str,
                  task: str, compact) -> int:
    output.mkdir(parents=True)
    (output / 'prompt.txt').write_text(prompt, encoding='utf-8')
    count = 0
    for start in range(0, len(records), batch_size):
        count += 1
        rows = [{'id': i + 1, **row} for i, row in enumerate(records[start:start + batch_size])]
        batch, stem = digest(rows), f'batch-{count:04d}'
        atomic_json(output / f'{stem}.manifest.json', {'format_version': 1, 'task': task, 'batch': batch, 'records': rows})
        schema = copy.deepcopy(RESULT_SCHEMA)
        schema['properties']['batch']['enum'] = [batch]
        atomic_json(output / f'{stem}.schema.json', schema)
        lines = [encoded({'batch': batch}), *(encoded(compact(row)) for row in rows)]
        (output / f'{stem}.input.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return count


def run_batches(directory: Path, catalog: Path = DEFAULT_CATALOG, model: str = DEFAULT_MODEL,
                reasoning: str = DEFAULT_REASONING, limit_batches: int | None = None,
                dry_run: bool = False, glosses: Path = DEFAULT_SUPPLEMENTS) -> dict:
    directory = directory.resolve()
    manifests = sorted(directory.glob('batch-*.manifest.json'))
    if not manifests or (limit_batches is not None and limit_batches < 1):
        raise ValueError('No batches found or invalid batch limit')
    completed = skipped = 0
    commands = []
    for manifest_path in manifests:
        manifest = read_json(manifest_path)
        stem = manifest_path.name.removesuffix('.manifest.json')
        result = directory / f'{stem}.result.json'
        if manifest.get('task') == 'content_reports':
            from app.report_review import validate_response
            validate = lambda path: validate_response(manifest, read_json(path))
        elif manifest.get('task') == 'sentences':
            from sentence_translation_exchange import validate_sentences
            validate = lambda path: validate_sentences(catalog, manifest_path, path, glosses)
        else:
            validate = lambda path: validated_entries(catalog, manifest_path, path)
        if result.exists():
            validate(result)
            skipped += 1
            continue
        if limit_batches is not None and completed >= limit_batches:
            break
        temporary = directory / f'{stem}.result.pending.json'
        command = ['codex', 'exec', '-', '-C', str(directory), '--model', model,
                   '-c', f'model_reasoning_effort="{reasoning}"', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '--ephemeral', '--json',
                   '--output-schema', str(directory / f'{stem}.schema.json'),
                   '--output-last-message', str(temporary)]
        commands.append(command)
        if not dry_run:
            prompt = (directory / 'prompt.txt').read_text(encoding='utf-8')
            prompt += (directory / f'{stem}.input.jsonl').read_text(encoding='utf-8')
            with (directory / f'{stem}.events.jsonl').open('w') as events:
                subprocess.run(command, input=prompt, text=True, stdout=events, check=True)
            validate(temporary)
            os.replace(temporary, result)
            atomic_json(directory / f'{stem}.run.json', {'model': model, 'reasoning_effort': reasoning,
                                                        'batch': manifest['batch']})
        completed += 1
    report = {'completed_batches': 0 if dry_run else completed, 'skipped_batches': skipped, 'model': model,
              'reasoning_effort': reasoning, 'dry_run': dry_run}
    if dry_run:
        report['planned_batches'] = completed
        report['commands'] = commands
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "import", "validate", "materialize"):
        command = commands.add_parser(name)
        command.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
        command.add_argument('--task', choices=('definitions', 'sentences'), default='definitions')
        command.add_argument('--glosses', type=Path, default=DEFAULT_SUPPLEMENTS)
        if name != "validate":
            command.add_argument("--supplements", type=Path)
        if name == "export":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--batch-size", type=int, default=100)
            command.add_argument("--limit", type=int)
            command.add_argument("--examples", action="store_true")
            command.add_argument("--retry-manifest", type=Path)
            command.add_argument("--retry-result", type=Path)
        elif name == "materialize":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--result", type=Path, required=True)
            if name == "import":
                command.add_argument("--dry-run", action="store_true")
    runner = commands.add_parser('run', help='Run exported batches with Codex; defaults to GPT-6 Luna / low')
    runner.add_argument('--directory', type=Path, required=True)
    runner.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    runner.add_argument('--glosses', type=Path, default=DEFAULT_SUPPLEMENTS)
    runner.add_argument('--model', default=DEFAULT_MODEL)
    runner.add_argument('--reasoning-effort', default=DEFAULT_REASONING, choices=('low', 'medium', 'high', 'xhigh', 'max'))
    runner.add_argument('--limit-batches', type=int)
    runner.add_argument('--dry-run', action='store_true')
    database = commands.add_parser('apply-db', help='Apply sentence supplements to an existing database with a backup')
    database.add_argument('--db', type=Path, required=True)
    database.add_argument('--supplements', type=Path)
    database.add_argument('--backup', type=Path)
    database.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'run':
            report = run_batches(args.directory, args.catalog, args.model, args.reasoning_effort,
                                 args.limit_batches, args.dry_run, args.glosses)
        elif args.command == 'apply-db':
            from sentence_translation_exchange import DEFAULT_SENTENCES
            from app.chinese_sentences import sync_database
            report = sync_database(args.db, args.supplements or DEFAULT_SENTENCES, args.backup, args.dry_run)
        elif args.task == 'sentences':
            from sentence_translation_exchange import (DEFAULT_SENTENCES, export_sentences, import_sentences,
                                                       materialize_sentences, validate_sentences)
            supplements = getattr(args, 'supplements', None) or DEFAULT_SENTENCES
            if args.command == 'export':
                report = export_sentences(args.catalog, supplements, args.output, args.batch_size, args.limit,
                                          args.glosses, args.retry_manifest, args.retry_result)
            elif args.command == 'import':
                report = import_sentences(args.catalog, supplements, args.manifest, args.result, args.dry_run, args.glosses)
            elif args.command == 'validate':
                entries, pending = validate_sentences(args.catalog, args.manifest, args.result, args.glosses)
                report = {'validated_examples': len(entries), 'pending_examples': pending}
            else:
                report = materialize_sentences(args.catalog, supplements, args.output)
        elif args.command == "export":
            args.supplements = args.supplements or DEFAULT_SUPPLEMENTS
            report = export_batches(args.catalog, args.supplements, args.output, args.batch_size, args.limit,
                                    args.examples, args.retry_manifest, args.retry_result)
        elif args.command == "import":
            report = import_result(args.catalog, args.supplements or DEFAULT_SUPPLEMENTS, args.manifest, args.result, args.dry_run)
        elif args.command == "validate":
            entries, pending = validated_entries(args.catalog, args.manifest, args.result)
            report = {"validated_senses": len(entries), "pending_senses": pending}
        else:
            report = materialize(args.catalog, args.supplements or DEFAULT_SUPPLEMENTS, args.output)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(encoded(report))


if __name__ == "__main__":
    main()
