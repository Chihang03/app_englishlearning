from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@unittest.skipUnless(importlib.util.find_spec('lxml'), 'optional Mac export dependency is not installed')
class MorphologyExportTests(unittest.TestCase):
    def record(self, word, markup, language='en', entry_id=None):
        entry_id = entry_id or word + '-entry'
        raw = f'<d:entry id="{entry_id}" d:title="{word}">{markup}</d:entry>'.encode()
        return {(language, entry_id): (word, raw)}

    def sense(self, word, key, family='verb', target=None):
        sentence = f'We have {target or word} this today.'
        return {'key': key, 'part_of_speech': family, 'definition_cn': '', 'definition_en': 'test meaning',
                'source': 'macOS Dictionary', 'examples': [{'sentence': sentence, 'target_form': target or word}]}

    def block(self, family, key, word, extra='', target=None):
        return f'<span class="se1"><span class="pos">{family}</span>{extra}<span class="msDict" id="{key}"><span class="df">test meaning</span><span class="eg"><span class="ex">We have {target or word} this today.</span></span></span></span>'

    def export(self, words, records):
        from build_morphology_bundle import catalog_evidence
        return catalog_evidence({w['word']: w for w in words}, records)

    def test_arbitrary_scoped_inflections_do_not_use_aliases_or_whitelist(self):
        words = [{'word': 'ride', 'senses': [self.sense('ride', 'en:ride-verb')]},
                 {'word': 'city', 'senses': [self.sense('city', 'en:city-noun', 'noun')]}]
        records = self.record('ride', self.block('verb', 'ride-verb', 'ride',
            '<span class="infg"><span class="sy">past</span><span class="inf">rode</span><span class="sy">past participle</span><span class="inf">ridden</span></span>'))
        records.update(self.record('city', self.block('noun', 'city-noun', 'city',
            '<span class="infg"><span class="sy">plural</span><span class="inf">cities</span></span>')))
        result = self.export(words, records)
        self.assertEqual({(f['unit_key'], f['spelling'], f['form_type']) for f in result['forms']},
                         {('ride:verb', 'rode', 'past'), ('ride:verb', 'ridden', 'past_participle'), ('city:noun', 'cities', 'plural')})
        self.assertTrue(all(f['evidence_kind'] == 'dictionary_inflection' for f in result['forms']))

    def test_example_evidence_is_exactly_sense_scoped_and_not_generated(self):
        words = [{'word': 'address', 'senses': [self.sense('address', 'en:verb', target='addressed'), self.sense('address', 'en:noun', 'noun')]}]
        records = self.record('address', self.block('verb', 'verb', 'address', target='addressed') + self.block('noun', 'noun', 'address'))
        forms = self.export(words, records)['forms']
        self.assertEqual({f['spelling'] for f in forms}, {'addressed'})
        self.assertTrue(all(f['unit_key'] == 'address:verb' and f['scope_sense_key'] == 'en:verb' for f in forms))
        words[0]['senses'][0]['examples'][0]['sentence'] = 'This invented example was addressed.'
        self.assertEqual(self.export(words, records)['forms'], [])

    def test_same_pos_homographs_keep_source_sense_scope(self):
        words = [{'word': 'lie', 'senses': [self.sense('lie', 'en:recline'), self.sense('lie', 'en:untruth')]}]
        records = self.record('lie', self.block('verb', 'recline', 'lie', '<span class="infg"><span class="sy">past</span><span class="inf">lay</span></span>'), entry_id='lie1')
        records.update(self.record('lie', self.block('verb', 'untruth', 'lie', '<span class="infg"><span class="inf">lied</span></span>'), entry_id='lie2'))
        forms = self.export(words, records)['forms']
        self.assertEqual({(f['spelling'], f['scope_sense_key']) for f in forms}, {('lay', 'en:recline'), ('lied', 'en:untruth')})
        self.assertFalse(any(f['scope_sense_key'] is None for f in forms))

    def test_reused_source_id_with_changed_meaning_is_not_verified(self):
        words = [{'word': 'address', 'senses': [self.sense('address', 'en:verb', target='addressed')]}]
        records = self.record('address', self.block('verb', 'verb', 'address', target='addressed'))
        words[0]['senses'][0]['definition_en'] = 'a different meaning under the old ID'
        result = self.export(words, records)
        self.assertEqual(result['lexical_units'], [])
        self.assertEqual(result['forms'], [])

    def test_grammar_reference_and_direct_adjective_coexist(self):
        words = [{'word': 'teach', 'senses': [self.sense('teach', 'en:teach')]},
                 {'word': 'taught', 'senses': [self.sense('taught', 'en:taught-adj', 'adjective')]}]
        records = self.record('teach', self.block('verb', 'teach', 'teach'))
        reference = '<span class="se1"><span class="pos">verb</span><span class="msDict" id="ref"><span class="xrg">past and past participle of <span class="xr"><a href="x-dictionary:r:teach-entry:NOAD:teach">teach</a></span></span></span></span>'
        records.update(self.record('taught', reference + self.block('adjective', 'taught-adj', 'taught')))
        result = self.export(words, records)
        self.assertEqual({f['form_type'] for f in result['forms']}, {'past', 'past_participle'})
        self.assertEqual({u['key'] for u in result['lexical_units']}, {'teach:verb', 'taught:adjective'})
        self.assertTrue(all(f['unit_key'] == 'teach:verb' for f in result['forms']))
        # The link's exact target entry is part of the proof.
        records[('en', 'taught-entry')] = ('taught', records[('en', 'taught-entry')][1].replace(b'teach-entry', b'wrong-entry'))
        self.assertEqual(self.export(words, records)['forms'], [])

    def test_derivative_needs_explicit_group_and_parent_pos(self):
        words = [{'word': 'rare', 'senses': [self.sense('rare', 'en:rare', 'adjective')]},
                 {'word': 'rarity', 'senses': [self.sense('rarity', 'en:rarity', 'noun')]}]
        derivative = '<span class="subEntryBlock t_derivatives"><span class="subEntry" id="derivative1"><span class="l">rarity</span><span class="pos">noun</span></span></span>'
        records = self.record('rare', self.block('adjective', 'rare', 'rare') + derivative)
        records.update(self.record('rarity', self.block('noun', 'rarity', 'rarity')))
        result = self.export(words, records)
        self.assertEqual([(r['from'], r['to'], r['relation_type']) for r in result['relations']], [('rare:adjective', 'rarity:noun', 'derived_noun')])
        records[('en', 'rare-entry')] = ('rare', records[('en', 'rare-entry')][1].replace(b't_derivatives', b't_phrases'))
        self.assertEqual(self.export(words, records)['relations'], [])
        records.update(self.record('rare', self.block('adjective', 'rare', 'rare') + self.block('verb', 'extra', 'rare') + derivative))
        self.assertEqual(self.export(words, records)['relations'], [])

    def test_etymology_and_unlabelled_irregular_are_not_inflections(self):
        words = [{'word': 'wish', 'senses': [self.sense('wish', 'en:wish')]}]
        markup = self.block('verb', 'wish', 'wish', '<span class="infg"><span class="inf">wuz</span></span>')
        markup += '<span class="etym"><span class="xrg">past of <a href="x-dictionary:r:wish-entry:NOAD:wish">wish</a></span></span>'
        self.assertEqual(self.export(words, self.record('wish', markup))['forms'], [])

    def test_derivative_without_examples_is_metadata_only(self):
        words = [{'word': 'rare', 'senses': [self.sense('rare', 'en:rare', 'adjective')]}]
        extra = '<span class="subEntryBlock t_derivatives"><span class="subEntry" id="derived"><span class="l">rarity</span><span class="pos">noun</span></span></span>'
        result = self.export(words, self.record('rare', self.block('adjective', 'rare', 'rare') + extra))
        unit = next(u for u in result['lexical_units'] if u['word'] == 'rarity')
        self.assertEqual(unit['expected_senses'], [])
        self.assertEqual(result['relations'][0]['relation_type'], 'derived_noun')
        self.assertEqual(result['coverage']['metadata_only_units'], 1)

    def test_sense_internal_plural_never_expands_to_all_noun_senses(self):
        words = [{'word': 'brother', 'senses': [self.sense('brother', 'en:religious', 'noun'), self.sense('brother', 'en:relative', 'noun')]}]
        markup = self.block('noun', 'religious', 'brother').replace('<span class="df">', '<span class="infg"><span class="sy">plural</span><span class="inf">brethren</span></span><span class="df">') + self.block('noun', 'relative', 'brother')
        result = self.export(words, self.record('brother', markup))
        self.assertEqual(result['forms'][0]['scope_sense_key'], 'en:religious')

    def test_full_export_works_with_empty_reviewed_list(self):
        from build_morphology_bundle import build_bundle
        from app.vocabulary_json import json_fingerprint
        words = [{'word': 'city', 'senses': [self.sense('city', 'en:city', 'noun')]}]
        records = self.record('city', self.block('noun', 'city', 'city', '<span class="infg"><span class="sy">plural</span><span class="inf">cities</span></span>'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps({'words': words}))
            overrides = root / 'overrides.json'
            overrides.write_text(json.dumps({'units': [], 'relations': [], 'reviewed_at': 'test'}))
            paths = {}
            for lang in ('en', 'zh'):
                resources = root / lang / 'Contents/Resources'
                resources.mkdir(parents=True)
                raw = records[('en', 'city-entry')][1] if lang == 'en' else b'<d:entry id="empty" d:title="unused"><span/></d:entry>'
                compressed = zlib.compress(raw)
                (resources / 'Body.data').write_bytes(bytes(96) + struct.pack('<I', len(compressed) + 8) + bytes(8) + compressed)
                (resources.parent / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': lang, 'CFBundleVersion': 'fixture'}))
                paths[lang] = resources.parent.parent
            output = root / 'forms.json'
            output.write_text(json.dumps({'words': {'city': {'verb': {'citying': ['ing']}}}}))
            build_bundle(catalog, output, root, overrides, paths)
            bundle = json.loads(output.read_text())
            self.assertEqual(bundle['forms'][0]['spelling'], 'cities')
            self.assertEqual(bundle['catalog_sha256'], json_fingerprint(catalog.read_bytes()))
            self.assertNotIn('verb', bundle['words']['city'])
            self.assertEqual(bundle['coverage']['catalog_headwords_scanned'], 1)
