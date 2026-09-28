"""Rebuild reviewed morphology from installed Mac records, retaining v1 hints.

No network, runtime NLP dependency or unscoped dictionary alias is used. Changed
reviewed records fail the build until their source evidence has been reviewed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import struct
import zlib

from lxml import html

from build_vocab_bundle import DICTIONARY_ASSETS, find_dictionary
from dictionary_senses import ancestor, nodes, parse_record, text
from app.morphology import validate_bundle
from app.word_forms import FORM_LABELS, pos_family


DATA = Path(__file__).resolve().parents[1] / 'data'
OVERRIDES = Path(__file__).with_name('morphology_overrides.json')


def dictionary_records(body: Path):
    """Read the installed dictionary's compressed entry blocks, without its index.

    Search-index aliases are deliberately unavailable through this path.
    """
    raw = body.read_bytes()
    offset, tail, count = 96, b'', 0
    while offset + 12 <= len(raw):
        size = struct.unpack_from('<I', raw, offset)[0]
        if size < 8 or offset + 4 + size > len(raw):
            break
        stream = tail + zlib.decompress(raw[offset + 12:offset + 4 + size])
        offset += 4 + size
        end = 0
        for match in re.finditer(rb'<d:entry\b.*?</d:entry>', stream, re.S):
            entry = match.group()
            end = match.end()
            title = re.search(rb'\bd:title="([^"]+)"', entry)
            identity = re.search(rb'\bid="([^"]+)"', entry)
            if title and identity:
                count += 1
                yield title[1].decode('utf-8'), identity[1].decode('utf-8'), entry
        tail = stream[end:]
    if count == 0:
        raise ValueError(f'Unsupported dictionary body: {body}')


def build_bundle(catalog_path: Path, output: Path, dictionary_root: Path,
                 overrides_path: Path = OVERRIDES, dictionary_paths: dict | None = None):
    overrides = json.loads(overrides_path.read_text(encoding='utf-8'))
    catalog_bytes = catalog_path.read_bytes()
    catalog = json.loads(catalog_bytes)
    catalog_words = {w['word']: w for w in catalog['words']}
    needed = {(u['source']['language'], u['source']['entry_id']) for u in overrides['units']}
    needed |= {(w['language'], w['entry_id']) for w in overrides.get('supplements', [])}
    records, manifests, families = {}, [], {}
    names = {'en': 'New Oxford American Dictionary.dictionary', 'zh': 'Simplified Chinese - English.dictionary'}
    for language, name in names.items():
        package = (dictionary_paths or {}).get(language) or find_dictionary(dictionary_root, name)
        body = package / 'Contents' / 'Resources' / 'Body.data'
        info = plistlib.loads((package / 'Contents' / 'Info.plist').read_bytes())
        manifests.append({'language': language, 'id': info.get('CFBundleIdentifier'),
                          'version': info.get('CFBundleVersion'), 'body_sha256': hashlib.sha256(body.read_bytes()).hexdigest()})
        for word, entry_id, raw in dictionary_records(body):
            if (language, entry_id) in needed:
                records[(language, entry_id)] = (word, raw)
            if word in catalog_words:
                root = html.fromstring(raw.decode('utf-8'))
                families.setdefault(word, set()).update(pos_family(text(p)) for cls in ('ps', 'pos') for p in nodes(root, cls)
                    if ancestor(p, ('subEntry',)) is None)
    units, forms, relations, supplemental = [], [], [], []
    for spec in overrides.get('supplements', []):
        word, raw = _checked_record(records, spec['language'], spec['entry_id'], spec['record_sha256'], spec['word'])
        parsed = parse_record(raw.decode('utf-8'), word, spec['language'], [word])
        senses = [s for s in parsed['senses'] if s['examples'] and s['key'] in spec['sense_keys']]
        if {s['key'] for s in senses} != set(spec['sense_keys']):
            raise ValueError('Supplemental source sense or example changed')
        item = {'word': word, 'senses': senses, 'pronunciation': parsed.get('pronunciation'),
                'memberships': spec['memberships']}
        supplemental.append(item)
        catalog_words.setdefault(word, item)
    for spec in overrides['units']:
        source = spec['source']
        word, raw = _checked_record(records, source['language'], source['entry_id'], source['record_sha256'], spec['word'])
        root = html.fromstring(raw.decode('utf-8'))
        source_pos = {pos_family(text(p)) for cls in ('pos', 'ps') for p in nodes(root, cls)
                      if ancestor(p, ('subEntry',)) is None}
        if spec['pos_group'] not in source_pos:
            raise ValueError(f'Source POS changed for {word}')
        item = catalog_words.get(word)
        senses = [s for s in (item or {}).get('senses', []) if pos_family(s['part_of_speech']) == spec['pos_group']]
        units.append({'key': spec['key'], 'word': word, 'pos_group': spec['pos_group'],
                      'provenance': source, 'expected_senses': [{'key': s['key'], 'definition_cn': s['definition_cn'],
                          'definition_en': s.get('definition_en')} for s in senses]})
        for form in spec.get('forms', []):
            if form['evidence_kind'] == 'dictionary_inflection':
                scoped = [n for n in nodes(root, 'infg') if ancestor(n, ('subEntry',)) is None
                          and _block_family(n) == spec['pos_group']]
                if not any(form['spelling'].casefold() == text(n).casefold() for group in scoped for n in nodes(group, 'inf')):
                    raise ValueError(f'Unattested scoped inflection: {word}/{form["spelling"]}')
            for kind in form['form_types']:
                forms.append({'unit_key': spec['key'], 'spelling': form['spelling'], 'form_type': kind,
                              'verification': 'verified', 'evidence_kind': form['evidence_kind'],
                              'provenance': {**source, 'review_note': form['review_note']}})
    keys = {u['key'] for u in units}
    for relation in overrides['relations']:
        if relation['from'] in keys and relation['to'] in keys:
            relations.append({**relation, 'verification': 'verified',
                              'provenance': {'evidence_kind': 'reviewed_override',
                                  'review_note': relation['review_note'], 'reviewed_at': overrides['reviewed_at']}})
    previous = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {'words': {}}
    hints = previous['words']
    # Remove the old adverb/verb pollution; these remain display hints only.
    for word, pos_groups in list(hints.items()):
        for family, values in list(pos_groups.items()):
            if word in families and family not in families[word]:
                del pos_groups[family]
                continue
            for spelling, kinds in list(values.items()):
                values[spelling] = [k for k in kinds if (
                    k not in ('comparative', 'superlative') or family in ('adjective', 'adverb'))]
                if not values[spelling]:
                    del values[spelling]
    bundle = {'format_version': 2, 'resolver_version': 'morphology-v1', 'source': 'macOS Dictionary',
              'catalog_sha256': hashlib.sha256(catalog_bytes).hexdigest(), 'dictionaries': manifests,
              'words': hints, 'lexical_units': units, 'forms': forms, 'relations': relations,
              'supplemental_words': supplemental}
    validate_bundle(bundle)
    output.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Exported {len(units)} reviewed units, {len(forms)} form links, {len(relations)} relations, {len(supplemental)} supplemental words')


def _block_family(node):
    block = ancestor(node, ('gramb', 'se1'))
    if block is None:
        return ''
    positions = [p for cls in ('ps', 'pos') for p in nodes(block, cls)
                 if ancestor(p, ('gramb', 'se1')) is block]
    return pos_family(text(positions[0])) if positions else ''


def _checked_record(records, language, entry_id, digest, expected_word):
    word, raw = records[(language, entry_id)]
    if word != expected_word or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f'Dictionary changed: review {language}:{entry_id}')
    return word, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=DATA / 'vocabulary_catalog.json')
    parser.add_argument('--output', type=Path, default=DATA / 'word_forms.json')
    parser.add_argument('--dictionary-root', type=Path, default=DICTIONARY_ASSETS)
    args = parser.parse_args()
    build_bundle(args.catalog, args.output, args.dictionary_root)


if __name__ == '__main__':
    main()
