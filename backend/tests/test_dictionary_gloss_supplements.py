import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from supplement_chinese_from_dictionary import match_sense


class DictionaryGlossSupplementTests(unittest.TestCase):
    def setUp(self):
        self.english = {"key": "en:bank", "part_of_speech": "名词", "definition_en": "land beside a river",
                        "definition_cn": "", "examples": [{"sentence": "We sat beside the bank."}]}
        self.chinese = {"key": "zh:bank", "part_of_speech": "名词", "definition_en": None,
                        "definition_cn": "河岸", "examples": []}

    def test_sole_meaning_in_both_complete_dictionaries_matches(self):
        self.assertEqual(match_sense(self.english, [self.chinese], [self.english])[1], "dictionary_single_sense")

    def test_hidden_meanings_without_examples_prevent_single_sense_match(self):
        other = {**self.english, "key": "en:finance", "definition_en": "a financial institution", "examples": []}
        self.assertIsNone(match_sense(self.english, [self.chinese], [self.english, other]))

    def test_multiple_chinese_meanings_prevent_single_sense_match(self):
        other = {**self.chinese, "key": "zh:finance", "definition_cn": "银行"}
        self.assertIsNone(match_sense(self.english, [self.chinese, other], [self.english]))

    def test_pos_and_source_definition_drift_are_rejected(self):
        self.assertIsNone(match_sense(self.english, [{**self.chinese, "part_of_speech": "动词"}], [self.english]))
        self.assertIsNone(match_sense(self.english, [self.chinese], [{**self.english, "definition_en": "money"}]))

    def test_exact_definition_or_sentence_establishes_link_amid_other_meanings(self):
        ambiguous = [self.english, {**self.english, "key": "en:other"}]
        chinese = {**self.chinese, "definition_en": "Land beside a river."}
        self.assertEqual(match_sense(self.english, [chinese], ambiguous)[1], "dictionary_exact_definition")
        chinese = {**self.chinese, "examples": [{"sentence": "we sat beside the bank"}]}
        original = copy.deepcopy(self.english)
        self.assertEqual(match_sense(self.english, [chinese], ambiguous)[1], "dictionary_exact_example")
        self.assertEqual(self.english, original)

    def test_conflicting_exact_matches_are_rejected(self):
        a = {**self.chinese, "definition_en": self.english["definition_en"], "examples": self.english["examples"]}
        b = {**a, "key": "zh:other", "definition_cn": "银行"}
        self.assertIsNone(match_sense(self.english, [a, b], [self.english]))


if __name__ == "__main__":
    unittest.main()
