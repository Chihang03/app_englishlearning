#!/usr/bin/env python3
"""Export a reproducible, attributed English frequency snapshot for local heads."""
from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import distribution
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.vocabulary_json import readable_json
from app.word_frequencies import frequency_tie_break, normalize_headword


ROOT = Path(__file__).resolve().parents[1]
WORDFREQ_VERSION = '3.1.1'


def export(catalog_path: Path, seed_path: Path, output_path: Path) -> dict:
    from wordfreq import tokenize, zipf_frequency

    package = distribution('wordfreq')
    if package.version != WORDFREQ_VERSION:
        raise ValueError(f'Install wordfreq=={WORDFREQ_VERSION} to reproduce this snapshot')
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    seed = json.loads(seed_path.read_text(encoding='utf-8')) if seed_path.exists() else []
    heads = sorted({normalize_headword(w['word']) for w in [*catalog['words'], *seed]})
    words = {}
    for head in heads:
        # Multi-token combinations receive heuristic estimates in wordfreq;
        # these are not direct observations of a compound's own frequency.
        score = zipf_frequency(head, 'en', wordlist='large') if len(tokenize(head, 'en')) == 1 else 0
        words[head] = round(score * 100) if score > 0 else None
    description = package.metadata['Description']
    notice = description.split('## License\n', 1)[1]
    payload = {
        'format_version': 1,
        'source': {
            'name': 'wordfreq', 'version': package.version, 'language': 'en',
            'wordlist': 'large', 'metric': 'Zipf', 'score_scale': 100,
            'usage_snapshot': 'through about 2021',
            'url': 'https://github.com/rspeer/wordfreq',
            'data_license': 'CC-BY-SA-4.0', 'library_license': 'Apache-2.0',
            'attribution': 'Robyn Speer (2022). rspeer/wordfreq v3.0. https://doi.org/10.5281/zenodo.7199437',
            'notice': notice,
        },
        'words': words,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(readable_json(payload), encoding='utf-8')
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT/'data'/'vocabulary_catalog.json')
    parser.add_argument('--seed', type=Path, default=ROOT/'data'/'seed_words.json')
    parser.add_argument('--output', type=Path, default=ROOT/'data'/'word_frequencies.json')
    parser.add_argument('--report', type=Path, default=ROOT/'data'/'word_frequency_report.json')
    args = parser.parse_args()
    payload = export(args.catalog, args.seed, args.output)
    measured = sum(score is not None for score in payload['words'].values())
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    scores = payload['words']
    lists = []
    for word_list in catalog['lists']:
        heads = {normalize_headword(word['word']) for word in catalog['words']
                 if any(m['list_id'] == word_list['id'] for m in word['memberships'])}
        ordered = sorted(heads, key=lambda word: (
            scores[word] is None, -(scores[word] or 0), frequency_tie_break(word)))
        lists.append({
            'list_id': word_list['id'], 'title': word_list['title'],
            'headword_count': len(heads),
            'measured_headword_count': sum(scores[word] is not None for word in heads),
            'missing_headwords': sorted(word for word in heads if scores[word] is None),
            'first_50': [{'word': word, 'zipf': scores[word]/100 if scores[word] is not None else None}
                         for word in ordered[:50]],
        })
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(readable_json({'format_version': 1, 'source': payload['source'],
                                         'lists': lists}), encoding='utf-8')
    print(f'Exported {len(payload["words"]):,} heads: {measured:,} with frequency, '
          f'{len(payload["words"])-measured:,} without a direct frequency')


if __name__ == '__main__':
    main()
