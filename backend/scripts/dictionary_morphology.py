"""Extract scoped grammatical evidence, never search-index aliases or etymology.

This is an offline Mac exporter. A rule may label a spelling already printed in
an inflection group, but may not invent a form or establish a derivative edge.
"""
from __future__ import annotations

import re

from dictionary_senses import ancestor, has, nodes, owned, text
from app.word_forms import pos_family, regular_form_types


WORD = re.compile(r"[A-Za-z][A-Za-z'-]*\Z")
BLOCKS = ('gramb', 'se1')
SENSES = ('semb', 'msDict')
TYPE_FAMILIES = {
    'plural': {'noun', 'pronoun', 'numeral'},
    'comparative': {'adjective', 'adverb'}, 'superlative': {'adjective', 'adverb'},
    'past': {'verb'}, 'past_participle': {'verb'}, 'ing': {'verb'},
    'present': {'verb'}, 'third_person_singular': {'verb'},
    'first_person_singular': {'verb'}, 'second_person_or_plural': {'verb'},
}


def label_types(label):
    label = label.strip().casefold()
    if 'etc' in label:
        return []  # umbrella headings can contain both -ing and -ed
    kinds = []
    if 'past participle' in label:
        kinds.append('past_participle')
    if re.search(r'\bpast\b', label.replace('past participle', '')):
        kinds.insert(0, 'past')
    if 'present participle' in label or label == 'gerund':
        kinds.append('ing')
    elif 'present' in label:
        kinds.append('present')
    if 'singular' in label and re.search(r'\b(?:third|3rd)\b', label):
        kinds = ['third_person_singular']
    if 'plural' in label:
        kinds.append('plural')
    if 'comparative' in label:
        kinds.append('comparative')
    if 'superlative' in label:
        kinds.append('superlative')
    return kinds


def block_family(block):
    positions = [p for cls in ('ps', 'pos') for p in owned(block, cls, BLOCKS)]
    families = {pos_family(text(p)) for p in positions} - {''}
    return next(iter(families)) if len(families) == 1 else ''


def sense_key(node, language):
    group = ancestor(node, SENSES)
    identity = (group.get('lexid') or group.get('id')) if group is not None else None
    return f'{language}:{identity}' if identity else None


def extract_record(root, word, language):
    forms, references, derivatives = [], [], []
    blocks = [b for cls in BLOCKS for b in nodes(root, cls)
              if ancestor(b, ('subEntry',)) is None]
    families = {block_family(b) for b in blocks} - {''}
    for block in blocks:
        family = block_family(block)
        constructions = [n for n in owned(block, 'frm', BLOCKS) if ancestor(n, SENSES) is None]
        if constructions and text(constructions[0]).removeprefix('to ').casefold() != word.casefold():
            continue
        for group in owned(block, 'infg', BLOCKS):
            if ancestor(group, ('subEntry', 'etym', 'etymg')) is not None or not family:
                continue
            kinds = []
            for node in group.iterdescendants():
                if has(node, 'gr') or has(node, 'sy'):
                    kinds = label_types(text(node))
                elif has(node, 'inf'):
                    spelling = text(node).strip()
                    if not WORD.fullmatch(spelling) or spelling.casefold() == word.casefold():
                        continue
                    # Explicitly printed, POS-scoped forms only. Unlabelled
                    # irregular forms remain unknown, rather than using suffixes.
                    types = kinds or regular_form_types(word, spelling, family)
                    types = [k for k in types if family in TYPE_FAMILIES[k]]
                    if types:
                        forms.append({'spelling': spelling, 'pos_group': family,
                                      'form_types': types, 'scope_sense_key': sense_key(group, language)})
        for reference in owned(block, 'xrg', BLOCKS):
            if ancestor(reference, ('subEntry', 'etym', 'etymg', 'eg', 'exg')) is not None:
                continue
            links = reference.xpath('.//a')
            if len(links) != 1:
                continue
            base = text(links[0]).strip()
            if not WORD.fullmatch(base) or base.casefold() == word.casefold():
                continue
            raw_label = text(reference).casefold()
            # Only a grammatical reference, not "variant of", "see", a phrase,
            # ordinary definition, or a root mentioned in an etymology.
            match = re.fullmatch(r'(.+?) of ' + re.escape(base.casefold()) + r'\.?', raw_label)
            if match:
                label = match[1]
            elif language == 'zh' and re.fullmatch(r'→\s*' + re.escape(base.casefold()) + r'(?:\s+[a-z],?(?:\s+[a-z])*)?', raw_label):
                labels = [text(n) for n in owned(block, 'gr', BLOCKS) if ancestor(n, SENSES) is None]
                label = ' '.join(labels)
            else:
                continue
            label = label.casefold().strip()
            if not re.fullmatch(r'(?:past|tense|and|participle|present|plural|comparative|superlative|third|3rd|person|singular|gerund|[ ,/-])+', label):
                continue
            kinds = label_types(label)
            inferred = ({'noun'} if 'plural' in kinds else
                        {'adjective', 'adverb'} if any(k in kinds for k in ('comparative', 'superlative')) else {'verb'})
            ref_family = family or (next(iter(inferred)) if len(inferred) == 1 else '')
            kinds = [k for k in kinds if ref_family in TYPE_FAMILIES[k]]
            href = links[0].get('href', '')
            entry_match = re.match(r'x-dictionary:r:([^:]+):', href)
            if kinds and entry_match:
                references.append({'base': base, 'base_entry_id': entry_match[1],
                                   'pos_group': ref_family, 'form_types': kinds})
    # A DERIVATIVES block is explicit evidence; PHRASES and etymology are not.
    for group in nodes(root, 't_derivatives'):
        parent = ancestor(group, BLOCKS)
        family = block_family(parent) if parent is not None else (next(iter(families)) if len(families) == 1 else '')
        if not family:
            continue  # the dictionary does not identify the parent's POS
        for entry in nodes(group, 'subEntry'):
            names = owned(entry, 'l', ('subEntry',))
            positions = owned(entry, 'pos', ('subEntry',))
            target_families = {pos_family(text(p)) for p in positions} - {''}
            if len(names) != 1 or len(target_families) != 1:
                continue
            spelling = text(names[0]).strip()
            if WORD.fullmatch(spelling) and spelling.casefold() != word.casefold():
                derivatives.append({'word': spelling, 'pos_group': next(iter(target_families)),
                                    'base_pos_group': family, 'subentry_id': entry.get('id')})
    return {'families': families, 'forms': forms, 'references': references, 'derivatives': derivatives}
