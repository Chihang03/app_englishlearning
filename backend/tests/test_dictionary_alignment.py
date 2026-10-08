from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def sense(key, gloss, examples=(), pos="名词", english=None):
    return {"key": key, "part_of_speech": pos, "definition_cn": gloss,
            "definition_en": english, "source": "macOS Dictionary",
            "examples": [{"sentence": sentence, "target_form": "daughter", "translation_cn": None}
                         for sentence in examples]}


class DictionaryAlignmentTests(unittest.TestCase):
    def test_reverse_example_completes_basic_sense_before_figurative_sense(self):
        from dictionary_alignment import align_senses
        basic = sense("zh:child", "女儿")
        figurative = sense("zh:church", "产物", ["a daughter of the Church."])
        reverse = {"key": "reverse:child", "definition_cn": "女儿", "pos_family": "noun",
                   "examples": [{"sentence": "Her daughter was chosen as Miss Hong Kong six years ago.",
                                 "target_form": "daughter", "translation_cn": "她女儿六年前当选为香港小姐。"}],
                   "provenance": {"entry_id": "z_id000963", "direction": "zh-en"}}
        entry = {"word": "daughter", "mac_zh": {"senses": [basic, figurative]}, "mac_reverse": [reverse]}
        chosen, gaps = align_senses(entry, {})
        self.assertEqual([s["key"] for s in chosen], ["zh:child", "zh:church"])
        self.assertEqual(chosen[0]["examples"][0]["provenance"]["entry_id"], "z_id000963")
        self.assertEqual(gaps, [])
        self.assertEqual(basic["examples"], [])  # Export alignment is repeatable.

    def test_chinese_usable_sense_does_not_hide_unmatched_english_sense(self):
        from dictionary_alignment import align_senses
        entry = {"word": "daughter", "mac_zh": {"senses": [sense("zh:church", "产物", ["a daughter of the Church."])]},
                 "mac_en": {"senses": [sense("en:descendant", "", ["We are the sons and daughters of Adam."],
                                                    english="a female descendant")]}}
        chosen, _ = align_senses(entry, {})
        self.assertEqual([s["key"] for s in chosen], ["zh:church", "en:descendant"])
        self.assertEqual(chosen[1]["definition_cn"], "")

    def test_missing_examples_are_reported_with_source_definition(self):
        from dictionary_alignment import align_senses
        chosen, gaps = align_senses({"word": "daughter", "mac_zh": {"senses": [sense("zh:child", "女儿")]}}, {})
        self.assertEqual(chosen, [])
        self.assertEqual((gaps[0]["key"], gaps[0]["definition_cn"], gaps[0]["reason"]),
                         ("zh:child", "女儿", "missing_example"))

    def test_reverse_examples_refuse_same_gloss_with_two_possible_senses(self):
        from dictionary_alignment import align_senses
        entry = {"word": "daughter", "mac_zh": {"senses": [sense("zh:1", "女儿"), sense("zh:2", "女儿")]},
                 "mac_reverse": [{"key": "reverse:child", "definition_cn": "女儿", "pos_family": "noun",
                                  "examples": [{"sentence": "Her daughter lives in London.", "target_form": "daughter"}],
                                  "provenance": {"entry_id": "reverse"}}]}
        chosen, gaps = align_senses(entry, {})
        self.assertEqual(chosen, [])
        self.assertEqual(gaps[-1]["reason"], "ambiguous_reverse_alignment")

    def test_traditional_matching_requires_unique_gloss_and_pos_in_both_sources(self):
        from dictionary_alignment import align_senses
        canonical = sense("zh:child", "女儿")
        entry = {"word": "daughter", "mac_zh": {"senses": [canonical]},
                 "mac_tw": {"senses": [sense("tw:1", "女儿、养女", ["Her daughter lives in London."]),
                                        sense("tw:2", "女儿", ["Our daughter lives at home."])]}}
        chosen, gaps = align_senses(entry, {})
        self.assertEqual(chosen, [])
        self.assertEqual(len([g for g in gaps if g["reason"] == "no_verified_alignment"]), 2)

    def test_original_bilingual_fallback_keeps_its_own_definition_and_key(self):
        from dictionary_alignment import align_senses
        chosen, _ = align_senses({"word": "daughter", "mac_tw": {"senses": [
            sense("tw:child", "女儿", ["Our daughter lives at home."])]}}, {})
        self.assertEqual(chosen[0]["key"], "tw:child")
        self.assertEqual(chosen[0]["definition_cn"], "女儿")

    def test_duplicate_example_preserves_existing_translation(self):
        from dictionary_alignment import align_senses
        canonical = sense("zh:child", "女儿", ["Her daughter lives in London."])
        canonical["examples"][0]["translation_cn"] = "已有核对的翻译。"
        incoming = sense("tw:child", "女儿", ["Her daughter lives in London."])
        incoming["examples"][0]["translation_cn"] = "词典的另一翻译。"
        chosen, _ = align_senses({"word": "daughter", "mac_zh": {"senses": [canonical]},
                                  "mac_tw": {"senses": [incoming]}}, {})
        self.assertEqual(len(chosen[0]["examples"]), 1)
        self.assertEqual(chosen[0]["examples"][0]["translation_cn"], "已有核对的翻译。")

    def test_new_cross_dictionary_match_keeps_historical_source_key_and_translation(self):
        from dictionary_alignment import align_senses, preserve_existing_senses
        old = sense("en:child", "女儿", ["Her daughter lives in London."], english="a female child")
        old["examples"][0]["translation_cn"] = "原先核对的翻译。"
        original = sense("en:child", "", ["Her daughter lives in London."], english="a female child")
        entry = {"word": "daughter", "mac_zh": {"senses": [sense("zh:child", "女儿", ["Her daughter lives in London."])]},
                 "mac_en": {"senses": [original]}}
        chosen, _ = align_senses(entry, {})
        retained = preserve_existing_senses(entry, chosen, [old])
        historical = next(s for s in retained if s["key"] == "en:child")
        self.assertEqual(historical["examples"][0]["translation_cn"], "原先核对的翻译。")
        original["definition_en"] = "a different meaning"
        with self.assertRaisesRegex(ValueError, "Historical dictionary source changed"):
            preserve_existing_senses(entry, chosen, [old])

    def test_checked_chinese_supplement_is_not_mistaken_for_dictionary_source_drift(self):
        from dictionary_alignment import preserve_existing_senses
        original = sense('zh:article', '', ['the daughter lives at home.'], pos='definite article', english='a specified thing')
        checked = sense('zh:article', '这；那', ['the daughter lives at home.'], pos='definite article', english='a specified thing')
        result = preserve_existing_senses({'word': 'the', 'mac_zh': {'senses': [original]}}, [original], [checked])
        self.assertEqual(result[0]['definition_cn'], '这；那')


@unittest.skipUnless(importlib.util.find_spec("lxml"), "optional Mac export dependencies")
class ReverseDictionaryParserTests(unittest.TestCase):
    def test_chinese_entry_reads_english_trans_and_chinese_ex_without_mixing_groups(self):
        from traditional_dictionary_senses import parse_reverse_record
        markup = '''<div id="child-entry"><span class="se1"><span class="se2">
          <span class="trans">a daughter</span><span class="eg"><span class="ex">她的女兒住在倫敦。</span>
          <span class="trans">Her daughter lives in London.</span></span></span>
          <span class="se2"><span class="trans">a girl</span><span class="eg"><span class="ex">女孩正在讀書。</span>
          <span class="trans">The girl is reading a book.</span></span></span></span></div>'''
        result = parse_reverse_record(markup, "女兒", {"daughter": {}, "girl": {}})
        self.assertEqual([s["word"] for s in result], ["daughter", "girl"])
        self.assertEqual(result[0]["examples"][0]["translation_cn"], "她的女兒住在倫敦。")
        self.assertEqual(result[0]["examples"][0]["sentence"], "Her daughter lives in London.")
        self.assertEqual(result[0]["pos_family"], "noun")

    def test_reverse_parser_rejects_phrases_missing_targets_and_subentries(self):
        from traditional_dictionary_senses import parse_reverse_record
        markup = '''<div><span class="se2"><span class="trans">daughter of the Church</span>
          <span class="eg"><span class="ex">一位信徒。</span><span class="trans">She is a daughter of the Church.</span></span></span>
          <span class="se2"><span class="trans">a daughter</span><span class="eg"><span class="ex">一個女孩。</span>
          <span class="trans">The girl lives in London.</span></span></span>
          <span class="subEntry"><span class="se2"><span class="trans">a daughter</span><span class="eg">
          <span class="ex">另一個子詞條。</span><span class="trans">Her daughter lives in London.</span></span></span></span></div>'''
        self.assertEqual(parse_reverse_record(markup, "女兒", {"daughter": {}}), [])

    def test_forward_parser_keeps_matching_example_translation_and_source(self):
        from traditional_dictionary_senses import parse_traditional_record
        markup = '''<div id="software-entry"><span class="se1"><span class="pos">n.</span>
          <span class="se2"><span class="trans">軟體</span><span class="eg"><span class="ex">My job is writing the software.</span>
          <span class="trans">我的工作是編寫軟體。</span></span></span></span></div>'''
        result = parse_traditional_record(markup, "software", ["software"])
        self.assertEqual(result[0]["examples"][0]["translation_cn"], "我的工作是編寫軟體。")
        self.assertEqual(result[0]["provenance"]["entry_id"], "software-entry")
