from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from translation_exchange import export_batches, import_result, materialize, validated_entries


class TranslationExchangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.catalog = self.folder / "catalog.json"
        self.supplements = self.folder / "supplements.json"
        self.output = self.folder / "batches"
        self.document = {"format_version": 2, "words": [{
            "word": "bank", "definition_cn": "", "senses": [
                {"key": "river", "part_of_speech": "名词", "definition_en": "land beside a river",
                 "definition_cn": "", "examples": [{"sentence": "We sat on the bank."}]},
                {"key": "river-duplicate", "part_of_speech": "名词", "definition_en": "land beside a river",
                 "definition_cn": "", "examples": []},
                {"key": "money", "part_of_speech": "名词", "definition_en": "a financial institution",
                 "definition_cn": "银行", "examples": []},
                {"key": "verb", "part_of_speech": "动词", "definition_en": "to deposit money",
                 "definition_cn": "", "examples": [{"sentence": "I bank my salary."}]},
            ],
        }]}
        self.write(self.catalog, self.document)
        self.write(self.supplements, {"format_version": 1, "entries": []})

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def export(self, **kwargs):
        report = export_batches(self.catalog, self.supplements, self.output, **kwargs)
        self.manifest = self.output / "batch-0001.manifest.json"
        self.result = self.folder / "result.json"
        self.batch = json.loads(self.manifest.read_text())["batch"]
        self.response = {"batch": self.batch, "items": [[1, "河岸"], [2, "存款"]]}
        self.write(self.result, self.response)
        return report

    def test_export_only_missing_senses_deduplicates_and_omits_examples(self):
        report = self.export()
        self.assertEqual((report["missing_senses"], report["unique_requests"]), (3, 2))
        lines = (self.output / "batch-0001.input.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[1]), [1, "bank", "名词", "land beside a river"])
        self.assertEqual(len(lines), 3)
        self.assertNotIn("river-duplicate", "\n".join(lines))
        with self.assertRaisesRegex(ValueError, "new output directory"):
            export_batches(self.catalog, self.supplements, self.output)

    def test_import_is_idempotent_and_materialization_preserves_other_content(self):
        self.export()
        previous_catalog = self.catalog.read_bytes()
        previous_supplements = self.supplements.read_bytes()
        report = import_result(self.catalog, self.supplements, self.manifest, self.result, dry_run=True)
        self.assertEqual(report["added_senses"], 3)
        self.assertEqual(self.supplements.read_bytes(), previous_supplements)
        report = import_result(self.catalog, self.supplements, self.manifest, self.result)
        self.assertEqual(Path(report["backup"]).read_bytes(), previous_supplements)
        self.assertEqual(import_result(self.catalog, self.supplements, self.manifest, self.result)["added_senses"], 0)
        self.assertEqual(self.catalog.read_bytes(), previous_catalog)
        generated = self.folder / "new-catalog.json"
        self.assertEqual(materialize(self.catalog, self.supplements, generated)["applied_senses"], 3)
        expected = copy.deepcopy(self.document)
        expected["words"][0]["definition_cn"] = "河岸"
        for sense, cn in zip(expected["words"][0]["senses"], ["河岸", "河岸", "银行", "存款"]):
            sense["definition_cn"] = cn
        self.assertEqual(json.loads(generated.read_text()), expected)
        cached = export_batches(self.catalog, self.supplements, self.folder / "cached")
        self.assertEqual(cached["exported_requests"], 0)
        with self.assertRaises(ValueError):
            materialize(self.catalog, self.supplements, self.catalog)

    def test_invalid_results_do_not_mutate_supplements(self):
        self.export()
        before = self.supplements.read_bytes()
        invalid = [
            {"batch": "wrong", "items": self.response["items"]},
            {"batch": self.batch, "items": [[1, "河岸"], [1, "岸"]]},
            {"batch": self.batch, "items": [[1, "河岸"], [3, "存款"]]},
            {"batch": self.batch, "items": [[1, "河岸"]]},
            {"batch": self.batch, "items": [[True, "河岸"], [2, "存款"]]},
            {"batch": self.batch, "items": [[1, "river bank"], [2, "存款"]]},
            {"batch": self.batch, "items": [[1, "待翻译"], [2, "存款"]]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write(self.result, value)
                with self.assertRaises(ValueError):
                    import_result(self.catalog, self.supplements, self.manifest, self.result)
                self.assertEqual(self.supplements.read_bytes(), before)

    def test_changed_english_pos_and_existing_chinese_rejected(self):
        self.export()
        for field, value in [("definition_en", "different meaning"), ("part_of_speech", "动词"),
                             ("definition_cn", "岸边")]:
            with self.subTest(field=field):
                document = copy.deepcopy(self.document)
                document["words"][0]["senses"][0][field] = value
                self.write(self.catalog, document)
                with self.assertRaises(ValueError):
                    validated_entries(self.catalog, self.manifest, self.result)

    def test_conflicting_overlay_rejected_without_partial_import(self):
        self.export()
        self.write(self.supplements, {"format_version": 1, "entries": [{
            "word": "bank", "sense_key": "verb", "expected_definition_en": "to deposit money",
            "expected_part_of_speech": "动词", "definition_cn": "把钱存入银行",
        }]})
        before = self.supplements.read_bytes()
        with self.assertRaisesRegex(ValueError, "Conflicting supplement"):
            import_result(self.catalog, self.supplements, self.manifest, self.result)
        self.assertEqual(self.supplements.read_bytes(), before)

    def test_null_retry_only_includes_unresolved_senses_with_example(self):
        self.export()
        self.response["items"][1][1] = None
        self.write(self.result, self.response)
        report = import_result(self.catalog, self.supplements, self.manifest, self.result)
        self.assertEqual((report["added_senses"], report["pending_senses"]), (2, 1))
        retry = self.folder / "retry"
        report = export_batches(self.catalog, self.supplements, retry, examples=True,
                                retry_manifest=self.manifest, retry_result=self.result)
        self.assertEqual(report["exported_requests"], 1)
        lines = (retry / "batch-0001.input.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[1]), [1, "bank", "动词", "to deposit money", "I bank my salary."])

    def test_manifest_tampering_and_invalid_limits_rejected(self):
        self.export()
        changed = json.loads(self.manifest.read_text())
        changed["records"][0]["keys"][0] = "money"
        self.write(self.manifest, changed)
        with self.assertRaisesRegex(ValueError, "manifest"):
            validated_entries(self.catalog, self.manifest, self.result)
        for options in [{"batch_size": 0}, {"limit": 0}, {"retry_manifest": self.manifest}]:
            with self.assertRaises(ValueError):
                export_batches(self.catalog, self.supplements, self.folder / "invalid", **options)


if __name__ == "__main__":
    unittest.main()
