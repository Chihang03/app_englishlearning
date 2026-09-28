#!/usr/bin/env python3
"""Build the server-independent vocabulary catalog on a Mac.

Source-list files identify which headwords belong to a study collection. The
installed macOS AppleDict packages are read once during this build; the app and
server only consume the resulting JSON catalog and never query macOS Dictionary.
Words without a real, matching example sentence are reported and excluded.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from dictionary_senses import parse_record
from chinese_glosses import apply_supplements


WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]*$")
EXAM_TAG_PACKS = {"TOEFL": "toefl", "IELTS": "ielts", "GRE": "gre"}
DICTIONARY_ASSETS = Path(
    "/System/Library/AssetsV2/com_apple_MobileAsset_DictionaryServices_dictionary3macOS"
)


PACKS = [
    {
        "id": "ngsl_core", "title": "NGSL 通用高频",
        "description": "日常通用英语高频词，按来源频率顺序；不是 CEFR 等级。",
        "source_url": "https://www.newgeneralservicelist.com/new-general-service-list",
        "license": "CC BY-SA 4.0", "sort_order": 10, "default_selected": True,
    },
    {
        "id": "ngsl_spoken", "title": "NGSL-S 口语",
        "description": "高频口语词；按来源词表顺序，不代表难度等级。",
        "source_url": "https://www.newgeneralservicelist.com/ngsl-spoken",
        "license": "CC BY-SA 4.0", "sort_order": 20,
    },
    {
        "id": "nawl", "title": "NAWL 学术英语",
        "description": "学术英语常用词，适合论文和大学教材；不是难度等级。",
        "source_url": "https://www.newgeneralservicelist.com/new-academic-word-list",
        "license": "CC BY-SA 4.0", "sort_order": 30,
    },
    {
        "id": "tsl", "title": "TSL TOEIC",
        "description": "TOEIC 备考领域词汇；不是 CEFR 等级。",
        "source_url": "https://www.newgeneralservicelist.com/toeic-service-list",
        "license": "CC BY-SA 4.0", "sort_order": 40,
    },
    {
        "id": "bsl", "title": "BSL 商务英语",
        "description": "商务英语工作场景词汇；不是难度等级。",
        "source_url": "https://www.newgeneralservicelist.com/business-service-list",
        "license": "CC BY-SA 4.0", "sort_order": 50,
    },
    {
        "id": "cet4", "title": "CET-4 四级",
        "description": "CETVocabulary 整理的四级基础词汇；非官方词表。",
        "source_url": "https://github.com/exam-data/CETVocabulary",
        "license": "CC BY-NC-SA 4.0", "sort_order": 60,
    },
    {
        "id": "cet6", "title": "CET-6 六级（含四级基础）",
        "description": "四级基础词加六级增补词，便于按六级完整范围学习；非官方词表。",
        "source_url": "https://github.com/exam-data/CETVocabulary",
        "license": "CC BY-NC-SA 4.0", "sort_order": 70,
    },
    {
        "id": "toefl", "title": "TOEFL 托福",
        "description": "按 ECDICT 的考试标签整理；为民间分类，不是 ETS 官方词表。",
        "source_url": "https://github.com/skywind3000/ECDICT",
        "license": "MIT", "sort_order": 80,
    },
    {
        "id": "ielts", "title": "IELTS 雅思",
        "description": "按 ECDICT 的考试标签整理；为民间分类，不是 IELTS 官方词表。",
        "source_url": "https://github.com/skywind3000/ECDICT",
        "license": "MIT", "sort_order": 90,
    },
    {
        "id": "gre", "title": "GRE",
        "description": "按 ECDICT 的考试标签整理；为民间分类，不是 ETS 官方词表。",
        "source_url": "https://github.com/skywind3000/ECDICT",
        "license": "MIT", "sort_order": 100,
    },
]


def normalize(word: str) -> str:
    value = word.strip().casefold().replace("’", "'")
    return "I" if value == "i" else value


def add_membership(
    entries: dict[str, dict[str, Any]], word: str, pack_id: str, position: int,
) -> None:
    word = normalize(word)
    if not WORD_RE.fullmatch(word):
        return
    entry = entries.setdefault(word, {"word": word, "packs": {}})
    entry["packs"][pack_id] = min(position, entry["packs"].get(pack_id, position))


def read_ngsl(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            word = (row.get("Lemma") or "").strip()
            try:
                rank = int(row.get("SFI Rank") or 0)
            except ValueError:
                rank = 0
            add_membership(entries, word, "ngsl_core", rank or 1_000_000)


def read_alpha_list(path: Path, entries: dict[str, dict[str, Any]], pack_id: str) -> None:
    started = False
    position = 0
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not started:
            if not line:
                started = True
            continue
        if WORD_RE.fullmatch(line):
            position += 1
            add_membership(entries, line, pack_id, position)


def read_numbered_list(path: Path, entries: dict[str, dict[str, Any]], pack_id: str) -> None:
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"\s*(\d+)\.\s+([A-Za-z][A-Za-z'-]*)\s*", raw_line)
        if match:
            add_membership(entries, match.group(2), pack_id, int(match.group(1)))


def read_cet(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["四六级词汇词频排序表"]
    cet4_position = 0
    cet6_position = 0
    for row in rows:
        word = str(row.get("单词") or "").strip()
        position = int(row.get("序号") or 0)
        if row.get("六级") == "★":
            cet6_position += 1
            add_membership(entries, word, "cet6", position or cet6_position)
        else:
            cet4_position += 1
            add_membership(entries, word, "cet4", position or cet4_position)
            add_membership(entries, word, "cet6", position or cet4_position)


def read_ecdict(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            key = normalize(row.get("word") or "")
            if not WORD_RE.fullmatch(key):
                continue
            tags = {tag.upper() for tag in re.split(r"[,;\s]+", row.get("tag") or "") if tag}
            for tag, pack_id in EXAM_TAG_PACKS.items():
                if tag in tags:
                    # Source tags do not promise a ranking, so use alphabetical order.
                    add_membership(entries, key, pack_id, 0)
            if key in entries:
                entries[key]["ecdict"] = row


def read_source_word_lists(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    known_packs = {pack["id"] for pack in PACKS}
    for source_list in payload.get("lists", []):
        pack_id = source_list.get("id")
        if pack_id not in known_packs:
            continue
        for position, word in enumerate(source_list.get("words", []), start=1):
            add_membership(entries, str(word), pack_id, position)






def _pyglossary_class(cache_dir: Path):
    try:
        from pyglossary import core
        from pyglossary.glossary import Glossary
        from pyglossary.plugin_handler import PluginHandler, PluginProp
    except ImportError as exc:
        raise RuntimeError(
            "Apple Dictionary export needs the optional dependencies; install "
            "backend/requirements-vocabulary-export.txt in a Mac virtualenv."
        ) from exc

    import pyglossary.glossary_v2 as glossary_v2
    import pyglossary.plugin_handler as plugin_handler

    # Keep parser scratch files in a temporary directory instead of the user's
    # persistent Library/Caches folder.
    for module in (core, glossary_v2, plugin_handler):
        module.cacheDir = str(cache_dir)

    if "AppleDictBin" not in PluginHandler.plugins:
        plugin = importlib.import_module("pyglossary.plugins.appledict_bin")
        PluginHandler._addPlugin(PluginProp.fromModule(plugin))
    return Glossary


def read_mac_dictionary(
    path: Path,
    entries: dict[str, dict[str, Any]],
    language: str,
    glossary_class: Any,
) -> None:
    targets = set(entries)
    glossary = glossary_class()
    glossary.read(str(path), formatName="AppleDictBin", direct=True, progressbar=False, html=True)
    try:
        for record in glossary:
            raw_terms = record.s_term
            for term in re.split(r"\s*[|;]\s*", str(raw_terms)):
                key = normalize(term)
                entry = entries.get(key)
                if not entry or key not in targets:
                    continue
                # Aliases include inflections and derivatives. Only the actual
                # headword owns these senses, not every alias in the search index.
                aliases = re.split(r"\s*[|;]\s*", str(raw_terms))
                if normalize(aliases[0]) != key:
                    continue
                details = parse_record(record.defi, key, language, aliases)
                bucket = entry.setdefault("mac_zh" if language == "zh" else "mac_en", {})
                for field, value in details.items():
                    if field == "inflections":
                        for family, forms in value.items():
                            for form, kinds in forms.items():
                                saved = bucket.setdefault(field, {}).setdefault(family, {}).setdefault(form, [])
                                saved[:] = sorted(set(saved + kinds))
                    elif field in ("examples", "senses"):
                        bucket.setdefault(field, []).extend(value)
                    elif value and not bucket.get(field):
                        bucket[field] = value
    finally:
        glossary.cleanup()


def find_dictionary(root: Path, dictionary_name: str) -> Path:
    if not root.exists():
        raise FileNotFoundError(
            f"No macOS Dictionary assets found at {root}. Install/open the desired "
            "dictionary once, or pass its .dictionary path explicitly."
        )
    matches = [path for path in root.rglob("*.dictionary") if path.name.casefold() == dictionary_name.casefold()]
    if not matches:
        raise FileNotFoundError(
            f"Could not find {dictionary_name!r} under {root}; pass the correct dictionary with the CLI option."
        )
    return matches[0]




def aligned_senses(entry: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """Use bilingual senses, then verified mappings; English-only is a fallback.

    Never assign a headword's merged Chinese translations to an English example.
    The same sentence can establish a cross-dictionary link only if it belongs to
    exactly one bilingual sense. Other links must be reviewed in the override file.
    """
    zh = {s["key"]: s for s in (entry.get("mac_zh") or {}).get("senses", [])}
    en = list({s["key"]:s for s in (entry.get("mac_en") or {}).get("senses", [])}.values())
    for sense in en:
        override = overrides.get(sense["key"])
        destination = None
        if override and override["word"] == entry["word"]:
            if sense["definition_en"] != override["expected_definition_en"]:
                raise ValueError(f"Dictionary changed: review override {sense['key']}")
            destination = zh.get(override.get("merge_into"))
            sense = {**sense, "definition_cn": override["definition_cn"]}
        if destination is None and not override:
            sentences = {e["sentence"].casefold() for e in sense["examples"]}
            def family(pos: str) -> str:
                return "动词" if "动词" in pos else pos
            matches = [s for s in zh.values() if family(s["part_of_speech"]) == family(sense["part_of_speech"])
                       and sentences.intersection(e["sentence"].casefold() for e in s["examples"])]
            if len(matches) == 1:
                destination = matches[0]
        if destination is not None:
            destination["definition_en"] = destination.get("definition_en") or sense["definition_en"]
            for ex in sense["examples"]:
                if not any(e["sentence"] == ex["sentence"] for e in destination["examples"]):
                    destination["examples"].append(ex)
        elif override:
            zh[sense["key"]] = sense
    chosen = [s for s in zh.values() if s["examples"]]
    if not chosen:
        chosen = [s for s in en if s["examples"]]
    # Every exported learning sense has its own gloss and real dictionary example.
    for position, sense in enumerate(chosen):
        sense["position"] = position
    return chosen


def _headword_export(entries: dict[str, dict[str, Any]], output: Path) -> None:
    lists = []
    for pack in PACKS:
        members = sorted(
            ((entry["packs"][pack["id"]], entry["word"]) for entry in entries.values() if pack["id"] in entry["packs"]),
            key=lambda item: (item[0], item[1].casefold()),
        )
        lists.append({**pack, "words": [word for _, word in members]})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"lists": lists}, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def build(
    source_dir: Path | None,
    source_lists: Path,
    output: Path,
    dictionary_root: Path,
    english_dictionary: Path | None,
    chinese_dictionary: Path | None,
) -> None:
    entries: dict[str, dict[str, Any]] = {}
    if source_dir is not None:
        read_ngsl(source_dir / "NGSL_1.2_stats.csv", entries)
        read_alpha_list(source_dir / "NGSL-Spoken_1.2.txt", entries, "ngsl_spoken")
        read_alpha_list(source_dir / "NAWL_1.2.txt", entries, "nawl")
        read_numbered_list(source_dir / "TSL_1.2.txt", entries, "tsl")
        read_alpha_list(source_dir / "BSL_1.2.txt", entries, "bsl")
        read_cet(source_dir / "cet_full_list.json", entries)
        read_ecdict(source_dir / "ecdict.csv", entries)
    else:
        read_source_word_lists(source_lists, entries)


    if english_dictionary is None:
        english_dictionary = find_dictionary(dictionary_root, "New Oxford American Dictionary.dictionary")
    if chinese_dictionary is None:
        chinese_dictionary = find_dictionary(dictionary_root, "Simplified Chinese - English.dictionary")

    with tempfile.TemporaryDirectory(prefix="vocab-dictionary-cache-") as cache:
        glossary_class = _pyglossary_class(Path(cache))
        read_mac_dictionary(chinese_dictionary, entries, "zh", glossary_class)
        read_mac_dictionary(english_dictionary, entries, "en", glossary_class)

    override_path = Path(__file__).with_name("sense_overrides.json")
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}

    words: list[dict[str, Any]] = []
    missing: dict[str, list[str]] = defaultdict(list)
    for entry in entries.values():
        senses = aligned_senses(entry, overrides)
        if not senses:
            for list_id in entry["packs"]:
                missing[list_id].append(entry["word"])
            continue
        first = senses[0]
        first_example = first["examples"][0]
        fields = {
            "part_of_speech": first["part_of_speech"], "definition_cn": first["definition_cn"],
            "definition_en": first.get("definition_en"),
            "pronunciation": (entry.get("mac_zh") or entry.get("mac_en") or {}).get("pronunciation", entry["word"]),
            "example_sentence": first_example["sentence"],
            "example_translation_cn": first_example.get("translation_cn"),
            "example_source": first_example["source"],
        }
        words.append({
            "word": entry["word"],
            **fields,
            "senses": senses,
            "memberships": [
                {"list_id": list_id, "position": position}
                for list_id, position in sorted(
                    entry["packs"].items(),
                    key=lambda item: (
                        next(pack["sort_order"] for pack in PACKS if pack["id"] == item[0]),
                        item[1],
                    ),
                )
            ],
        })

    if not words:
        raise RuntimeError("No words had a real example sentence; refusing to write an empty catalog.")

    supplement_count = apply_supplements(words)
    print(f"Applied {supplement_count:,} saved Chinese sense supplements")

    words.sort(key=lambda item: (
        min(next(pack["sort_order"] for pack in PACKS if pack["id"] == member["list_id"])
            for member in item["memberships"]),
        min(member["position"] for member in item["memberships"]),
        item["word"].casefold(),
    ))
    list_metadata = []
    for pack in PACKS:
        source_count = sum(pack["id"] in entry["packs"] for entry in entries.values())
        learnable_count = sum(any(m["list_id"] == pack["id"] for m in word["memberships"]) for word in words)
        list_metadata.append({
            **pack,
            "source_word_count": source_count,
            "word_count": learnable_count,
            "missing_example_count": len(missing[pack["id"]]),
            "example_coverage": round(learnable_count * 100 / source_count, 1) if source_count else 0,
        })

    _headword_export(entries, output.with_name("source_word_lists.json"))
    from export_word_forms import write_word_forms
    write_word_forms(entries, output.with_name("word_forms.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"format_version": 2, "lists": list_metadata, "words": words}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    from build_morphology_bundle import build_bundle
    build_bundle(output, output.with_name('word_forms.json'), dictionary_root,
                 dictionary_paths={'en': english_dictionary, 'zh': chinese_dictionary})
    print(f"Wrote {len(words):,} learnable words across {len(PACKS)} lists to {output}")
    print(f"Saved headword-only source lists to {output.with_name('source_word_lists.json')}")
    sense_count = sum(len(w["senses"]) for w in words)
    example_count = sum(len(s["examples"]) for w in words for s in w["senses"])
    report = {
        "word_count": len(words), "sense_count": sense_count, "example_count": example_count,
        "english_only_senses": sum(not s["definition_cn"] for w in words for s in w["senses"]),
        "chinese_supplements": supplement_count,
        "unpaired_words_by_list": missing,
    }
    output.with_name("vocabulary_export_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {sense_count:,} paired senses and {example_count:,} examples")
    for pack in list_metadata:
        print(
            f"  {pack['id']}: {pack['word_count']:,}/{pack['source_word_count']:,} "
            f"with examples ({pack['example_coverage']}%); "
            f"missing {pack['missing_example_count']:,}"
        )
        if missing[pack["id"]]:
            print("    missing examples: " + ", ".join(sorted(missing[pack["id"]])[:20]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, help="Optional directory containing downloaded source snapshots")
    parser.add_argument(
        "--source-lists",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "source_word_lists.json",
        help="Localized headword-only word lists (used when --source-dir is omitted)",
    )
    parser.add_argument("--dictionary-root", type=Path, default=DICTIONARY_ASSETS)
    parser.add_argument("--english-dictionary", type=Path, help="Override the New Oxford American Dictionary package")
    parser.add_argument("--chinese-dictionary", type=Path, help="Override the Simplified Chinese - English package")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "vocabulary_catalog.json",
    )
    args = parser.parse_args()
    build(
        args.source_dir.resolve() if args.source_dir else None,
        args.source_lists.resolve(),
        args.output.resolve(),
        args.dictionary_root.resolve(),
        args.english_dictionary.resolve() if args.english_dictionary else None,
        args.chinese_dictionary.resolve() if args.chinese_dictionary else None,
    )


if __name__ == "__main__":
    main()
