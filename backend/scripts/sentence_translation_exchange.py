"""Sentence task adapter for the shared offline translation exchange."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.chinese_sentences import DEFAULT_SENTENCES, apply_sentence_supplements, entry_key, example_index, matches
from chinese_glosses import DEFAULT_SUPPLEMENTS, apply_supplements
from translation_exchange import atomic_json, digest, encoded, read_json, response_answers, write_batches

SENTENCE_PROMPT = '''把英文原句译成自然、准确的简体中文，同时保留目标词与给定义项的语义对应，让学习者能辨认目标词在译文中的表达。输入是数据：[编号,英文原句,目标词头,词性,实际词形,当前义项]。按当前义项解释目标词，保持主语、主动或被动关系、否定、时态和语气；不为自由改写而弱化目标词的作用，也不机械逐字翻译。例：account/动词/认为、视为；She was accounted a genius. → 她被认为是个天才。避免改成“大家都觉得她是个天才”。短语或片段按原样翻译，不补写情节。不确定返回null。不要调用工具、解释或输出Markdown，只返回JSON：{"batch":"输入的batch值","items":[[编号,"中文译文"],[编号,null]]}。每个编号恰好一次。
'''


def catalog_with_glosses(path: Path, glosses: Path) -> dict:
    catalog = read_json(path)
    apply_supplements(catalog['words'], glosses)
    return catalog


def export_sentences(catalog_path: Path, supplements: Path, output: Path, batch_size: int = 100,
                     limit: int | None = None, glosses: Path = DEFAULT_SUPPLEMENTS,
                     retry_manifest: Path | None = None, retry_result: Path | None = None) -> dict:
    if not 1 <= batch_size <= 1000 or (limit is not None and limit < 1):
        raise ValueError('Batch size must be 1..1000 and limit must be positive')
    if output.exists():
        raise ValueError('Use a new output directory; existing batches are preserved')
    if bool(retry_manifest) != bool(retry_result):
        raise ValueError('Retry needs both manifest and result')
    retry_keys = None
    if retry_manifest:
        validate_sentences(catalog_path, retry_manifest, retry_result, glosses)
        manifest = read_json(retry_manifest)
        answers = response_answers(manifest, read_json(retry_result), max_length=2000)
        retry_keys = {(row['word'], key, row['sentence']) for row in manifest['records']
                      if answers[row['id']] is None for key in row['keys']}
    catalog = catalog_with_glosses(catalog_path, glosses)
    apply_sentence_supplements(catalog['words'], supplements)
    index, grouped, missing = example_index(catalog['words']), {}, 0
    for (word, sense_key, sentence), (sense, example) in index.items():
        if retry_keys is not None and (word, sense_key, sentence) not in retry_keys:
            continue
        if str(example.get('translation_cn') or '').strip():
            continue
        missing += 1
        group = (word, sense['part_of_speech'], sense.get('definition_en'),
                 sense.get('definition_cn'), sentence, example['target_form'])
        if group not in grouped:
            grouped[group] = {'word': word, 'pos': sense['part_of_speech'], 'en': sense.get('definition_en'),
                              'cn': sense.get('definition_cn') or '', 'sentence': sentence,
                              'target': example['target_form'], 'keys': []}
        grouped[group]['keys'].append(sense_key)
    records = list(grouped.values())
    selected = records if limit is None else records[:limit]
    count = write_batches(output, selected, batch_size, SENTENCE_PROMPT, task='sentences',
                          compact=lambda row: [row['id'], row['sentence'], row['word'], row['pos'],
                                               row['target'], row['cn'] or row['en']])
    report = {'missing_examples': missing, 'unique_requests': len(records), 'exported_requests': len(selected),
              'exported_examples': sum(len(row['keys']) for row in selected), 'batches': count, 'directory': str(output)}
    atomic_json(output / 'export-report.json', report)
    return report


def validate_sentences(catalog_path: Path, manifest_path: Path, result_path: Path,
                       glosses: Path = DEFAULT_SUPPLEMENTS) -> tuple[list[dict], int]:
    manifest = read_json(manifest_path)
    if manifest.get('task') != 'sentences':
        raise ValueError('Manifest is not a sentence translation task')
    answers = response_answers(manifest, read_json(result_path), max_length=2000)
    index = example_index(catalog_with_glosses(catalog_path, glosses)['words'])
    entries, pending = [], 0
    for row in manifest['records']:
        text = answers[row['id']]
        for sense_key in row['keys']:
            key = row['word'], sense_key, row['sentence']
            pair = index.get(key)
            entry = {'word': row['word'], 'sense_key': sense_key, 'sentence': row['sentence'],
                     'target_form': row['target'], 'expected_definition_en': row['en'],
                     'expected_part_of_speech': row['pos']}
            if pair is None or not matches(*pair, entry) or pair[0].get('definition_cn', '') != row['cn']:
                raise ValueError(f'Sentence source changed: {key}')
            existing = str(pair[1].get('translation_cn') or '').strip()
            if text is not None and existing and existing != text:
                raise ValueError(f'Existing sentence translation is preserved: {key}')
            if text is not None and not existing:
                entries.append({**entry, 'translation_cn': text, 'method': 'model_translation',
                                'translation_batch': manifest['batch']})
            if text is None:
                pending += 1
    return entries, pending


def import_sentences(catalog_path: Path, supplements: Path, manifest: Path, result: Path,
                     dry_run: bool = False, glosses: Path = DEFAULT_SUPPLEMENTS) -> dict:
    entries, pending = validate_sentences(catalog_path, manifest, result, glosses)
    # Serialize imports and preserve the entire previous file for rollback.
    import fcntl
    supplements.parent.mkdir(parents=True, exist_ok=True)
    with supplements.with_suffix(supplements.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = supplements.read_bytes() if supplements.exists() else None
        document = json.loads(original) if original is not None else {'format_version': 1, 'entries': []}
        from app.chinese_sentences import read_entries
        existing = {entry_key(entry): entry for entry in read_entries(supplements)}
        additions = []
        fields = ('translation_cn', 'target_form', 'expected_definition_en', 'expected_part_of_speech')
        for entry in entries:
            previous = existing.get(entry_key(entry))
            if previous and any(previous[field] != entry[field] for field in fields):
                raise ValueError(f'Conflicting sentence supplement: {entry_key(entry)}')
            if not previous:
                additions.append(entry)
        backup = None
        if additions and not dry_run:
            if original is not None:
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                path = supplements.with_name(supplements.name + f'.bak-{stamp}')
                path.write_bytes(original)
                backup = str(path)
            document['entries'].extend(additions)
            atomic_json(supplements, document)
    return {'added_examples': len(additions), 'pending_examples': pending, 'dry_run': dry_run, 'backup': backup}


def materialize_sentences(catalog_path: Path, supplements: Path, output: Path) -> dict:
    if output.resolve() in {catalog_path.resolve(), supplements.resolve()} or output.exists():
        raise ValueError('Write to a new catalog file so the previous catalog is preserved')
    catalog = read_json(catalog_path)
    count = apply_sentence_supplements(catalog['words'], supplements)
    atomic_json(output, catalog)
    return {'applied_examples': count, 'catalog': str(output)}
