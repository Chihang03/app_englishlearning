"""Export morphology for the whole catalog from installed Mac records.

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
from dictionary_morphology import extract_record
from app.morphology import validate_bundle
from app.word_forms import pos_family, regular_form_types


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
            # Also read grammatical reference headwords absent from the catalog.
            # Unfiltered aliases are never inputs to the verified extractor.
            reference = b'xrg' in raw and any(label in raw for label in
                (b'past', b'participle', b'plural', b'comparative', b'superlative'))
            if (language, entry_id) in needed or word in catalog_words or reference:
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
    automatic = catalog_evidence(catalog_words, records)
    # Reviewed exceptions still override a duplicate automatic edge. They are
    # supplements to full-catalog extraction, not the list driving extraction.
    units = _unique([*units, *automatic['lexical_units']], lambda u: u['key'])
    forms = _unique([*forms, *automatic['forms']], lambda f:
        (f['unit_key'], f['spelling'].casefold(), f['form_type'], f.get('scope_sense_key')))
    relations = _unique([*relations, *automatic['relations']], lambda r:
        (r['from'], r['to'], r['relation_type']))
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
    bundle = {'format_version': 2, 'resolver_version': 'morphology-v2-catalog', 'source': 'macOS Dictionary',
              'catalog_sha256': hashlib.sha256(catalog_bytes).hexdigest(), 'dictionaries': manifests,
              'words': hints, 'lexical_units': units, 'forms': forms, 'relations': relations,
              'supplemental_words': supplemental, 'coverage': automatic['coverage']}
    validate_bundle(bundle)
    output.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Exported {len(units)} evidenced units, {len(forms)} form links, {len(relations)} relations, {len(supplemental)} supplemental words')


def _unique(values, key):
    seen = set()
    result = []
    for value in values:
        identity = key(value)
        if identity not in seen:
            result.append(value)
            seen.add(identity)
    return result


def _same_source_sense(original, catalog_sense, language):
    if original is None or pos_family(original['part_of_speech']) != pos_family(catalog_sense['part_of_speech']):
        return False
    # Dictionary updates may reuse a sense ID. Match its original-language
    # definition as well, without requiring a supplemental translation to be
    # identical to dictionary text.
    field = 'definition_en' if language == 'en' else 'definition_cn'
    return bool(original.get(field)) and original[field] == catalog_sense.get(field)


def catalog_evidence(catalog_words, records):
    """Conservative catalog-wide extraction, also usable with fixture records.

    Same-POS homographs need exact source sense scope. A reference to a different
    dictionary entry, an unlabelled irregular form, or a derivative with no
    identifiable parent POS is ignored; no string-only merge is permitted.
    """
    inspected, matched, origins = {}, {}, {}
    for (language, entry_id), (word, raw) in sorted(records.items()):
        root = html.fromstring(raw.decode('utf-8'))
        facts = extract_record(root, word, language)
        item = catalog_words.get(word)
        source = {'language': language, 'entry_id': entry_id,
                  'record_sha256': hashlib.sha256(raw).hexdigest()}
        aliases = [word, *(e['target_form'] for s in (item or {}).get('senses', []) for e in s['examples'])]
        parsed = parse_record(raw.decode('utf-8'), word, language, aliases) if item else {'senses': []}
        source_senses = {s['key']: s for s in parsed['senses']}
        matched_keys = set()
        for family in facts['families']:
            origins.setdefault((word, family, language), set()).add(entry_id)
        for sense in (item or {}).get('senses', []):
            family = pos_family(sense['part_of_speech'])
            original = source_senses.get(sense['key'])
            if family and _same_source_sense(original, sense, language):
                matched.setdefault((word, family), []).append(source)
                matched_keys.add(sense['key'])
        inspected[(language, entry_id)] = {'word': word, 'facts': facts, 'source': source,
                                          'source_senses': source_senses, 'matched_keys': matched_keys}
    units = {}
    for (word, family), sources in sorted(matched.items()):
        key = f'{word}:{family}'
        units[key] = {'key': key, 'word': word, 'pos_group': family,
                      'provenance': {'evidence_kind': 'dictionary_sense',
                                     'records': _unique(sources, lambda s: (s['language'], s['entry_id']))},
                      'expected_senses': [{'key': s['key'], 'definition_cn': s['definition_cn'],
                          'definition_en': s.get('definition_en')} for s in catalog_words[word]['senses']
                          if pos_family(s['part_of_speech']) == family]}
    forms, relations = [], []
    skipped = {'unscoped_homographs': 0, 'unresolved_references': 0}

    def scopes(record, family, specific=None):
        word = record['word']
        unit = units.get(f'{word}:{family}')
        if not unit:
            return []
        expected = {s['key'] for s in unit['expected_senses']}
        if specific:
            return [specific] if specific in expected and specific in record['matched_keys'] else []
        ambiguous = any(len(origins.get((word, family, lang), set())) > 1 for lang in ('en', 'zh'))
        if not ambiguous:
            return [None]
        exact = sorted(expected.intersection(record['matched_keys']))
        if not exact:
            skipped['unscoped_homographs'] += 1
        return exact

    def add_form(unit_key, spelling, kinds, source, evidence_kind, scope=None):
        for kind in kinds:
            forms.append({'unit_key': unit_key, 'spelling': spelling, 'form_type': kind,
                          'verification': 'verified', 'evidence_kind': evidence_kind,
                          'provenance': source, **({'scope_sense_key': scope} if scope else {})})

    for (language, _), record in inspected.items():
        word, facts, source = record['word'], record['facts'], record['source']
        for form in facts['forms']:
            for scope in scopes(record, form['pos_group'], form['scope_sense_key']):
                add_form(f'{word}:{form["pos_group"]}', form['spelling'], form['form_types'],
                         source, 'dictionary_inflection', scope)
        for reference in facts['references']:
            target = inspected.get((language, reference['base_entry_id']))
            if target is None or target['word'].casefold() != reference['base'].casefold():
                skipped['unresolved_references'] += 1
                continue
            family = reference['pos_group']
            for scope in scopes(target, family):
                add_form(f'{target["word"]}:{family}', word, reference['form_types'],
                         {**source, 'target': target['source']}, 'dictionary_grammar_reference', scope)
        for derivative in facts['derivatives']:
            base_key = f'{word}:{derivative["base_pos_group"]}'
            target_key = f'{derivative["word"]}:{derivative["pos_group"]}'
            kind = 'derived_' + derivative['pos_group']
            if base_key in units and target_key not in units:
                target_senses = [s for s in catalog_words.get(derivative['word'], {}).get('senses', [])
                                 if pos_family(s['part_of_speech']) == derivative['pos_group']]
                if not target_senses:
                    # Many real DERIVATIVES subentries have no separate example-
                    # complete catalog entry. Preserve the relationship as
                    # metadata, without inventing a definition, example or SRS.
                    units[target_key] = {'key': target_key, 'word': derivative['word'],
                        'pos_group': derivative['pos_group'], 'expected_senses': [],
                        'provenance': {**source, 'evidence_kind': 'dictionary_derivative',
                                       'subentry_id': derivative['subentry_id']}}
            if base_key in units and target_key in units and kind in ('derived_noun', 'derived_adjective', 'derived_adverb', 'derived_verb'):
                # A POS-wide relation cannot identify a same-POS homonym.
                if any(len(origins.get((headword, family, lang), set())) > 1
                       for headword, family in ((word, derivative['base_pos_group']),
                                               (derivative['word'], derivative['pos_group'])) for lang in ('en', 'zh')):
                    continue
                relations.append({'from': base_key, 'to': target_key, 'relation_type': kind,
                    'verification': 'verified', 'provenance': {**source, 'evidence_kind': 'dictionary_derivative',
                                                             'subentry_id': derivative['subentry_id']}})
    forms = _unique(forms, lambda f: (f['unit_key'], f['spelling'].casefold(), f['form_type'], f.get('scope_sense_key')))
    broad = {(f['unit_key'], f['spelling'].casefold(), f['form_type']) for f in forms if not f.get('scope_sense_key')}
    # A real example tied to an exact source sense can attest a regular form.
    # The rule labels the attested target; it neither generates spellings nor
    # grants that form to other meanings, POS families or etymological roots.
    for record in inspected.values():
        word, source = record['word'], record['source']
        if word not in catalog_words:
            continue
        for sense in catalog_words[word]['senses']:
            family = pos_family(sense['part_of_speech'])
            key = f'{word}:{family}'
            original = record['source_senses'].get(sense['key'])
            if key not in units or sense['key'] not in record['matched_keys']:
                continue
            attested = {(e['sentence'], e['target_form']) for e in original['examples']}
            for example in sense['examples']:
                if (example['sentence'], example['target_form']) not in attested:
                    continue
                kinds = regular_form_types(word, example['target_form'], family)
                kinds = [k for k in kinds if (key, example['target_form'].casefold(), k) not in broad]
                add_form(key, example['target_form'], kinds,
                         {**source, 'source_sense_key': sense['key'], 'sentence': example['sentence']},
                         'dictionary_example', sense['key'])
    forms = _unique(forms, lambda f: (f['unit_key'], f['spelling'].casefold(), f['form_type'], f.get('scope_sense_key')))
    return {'lexical_units': list(units.values()), 'forms': forms, 'relations': relations,
            'coverage': {'catalog_headwords_scanned': len(catalog_words),
                         'headwords_with_source_identity': len({u['word'] for u in units.values() if u['expected_senses']}),
                         'metadata_only_units': sum(not u['expected_senses'] for u in units.values()),
                         'automatic_form_links': len(forms), 'automatic_relations': len(relations),
                         'skipped': skipped}}


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
