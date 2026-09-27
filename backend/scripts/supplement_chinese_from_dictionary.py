#!/usr/bin/env python3
"""Recover Chinese glosses from installed dictionaries without translation models."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

DEFAULT_EXCLUSIONS = Path(__file__).with_name("dictionary_gloss_exclusions.json")


def exclude_known_mismatches(entries: list[dict], path: Path = DEFAULT_EXCLUSIONS) -> list[dict]:
    exclusions = json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else []
    rejected = {(e["word"], e["sense_key"], e["dictionary"]): e for e in exclusions}
    result = []
    for entry in entries:
        rejection = rejected.get((entry["word"], entry["sense_key"], entry["dictionary"]))
        if rejection:
            for field in ("expected_definition_en", "expected_part_of_speech"):
                if entry[field] != rejection[field]:
                    raise ValueError(f"Dictionary changed; review exclusion: {entry['word']}")
        else:
            result.append(entry)
    return result

def normalized(value: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (value or "").casefold()).strip()


def pos_family(value: str) -> str:
    return "动词" if value in ("及物动词", "不及物动词", "反身动词") else value


def match_sense(sense: dict, chinese: list[dict], english: list[dict]) -> tuple[dict, str] | None:
    """Require an exact definition/example or one meaning in both source records.

    Count ALL source meanings, including meanings with no usable example. The
    smaller learning catalog cannot establish that a word is unambiguous.
    """
    family = pos_family(sense["part_of_speech"])
    candidates = [s for s in chinese if s.get("definition_cn") and pos_family(s["part_of_speech"]) == family]
    exact = [s for s in candidates if normalized(s.get("definition_en"))
             and normalized(s.get("definition_en")) == normalized(sense.get("definition_en"))]
    if exact and len({s["definition_cn"] for s in exact}) == 1:
        return exact[0], "dictionary_exact_definition"
    examples = {normalized(e["sentence"]) for e in sense["examples"]}
    same = [s for s in candidates if examples.intersection(normalized(e["sentence"]) for e in s["examples"])]
    if len(same) == 1:
        return same[0], "dictionary_exact_example"
    source_meanings = [s for s in english if pos_family(s["part_of_speech"]) == family]
    if len(candidates) == len(source_meanings) == 1:
        original = source_meanings[0]
        if original["key"] == sense["key"] and original.get("definition_en") == sense.get("definition_en"):
            return candidates[0], "dictionary_single_sense"
    return None


def collect(catalog: dict, sources: dict, dictionary: str = "macOS Simplified Chinese - English") -> list[dict]:
    entries = []
    for word in catalog["words"]:
        source = sources.get(word["word"].casefold(), {})
        chinese = source.get("mac_zh", {}).get("senses", [])
        english = source.get("mac_en", {}).get("senses", [])
        for sense in word["senses"]:
            if str(sense.get("definition_cn") or "").strip():
                continue
            match = match_sense(sense, chinese, english)
            if match is None:
                continue
            selected, method = match
            entries.append({"word": word["word"], "sense_key": sense["key"],
                "expected_definition_en": sense.get("definition_en"),
                "expected_part_of_speech": sense["part_of_speech"],
                "definition_cn": selected["definition_cn"], "method": method,
                "dictionary": dictionary,
                "dictionary_sense_key": selected["key"],
                "dictionary_definition_en": selected.get("definition_en")})
    return entries


def main() -> None:
    from build_vocab_bundle import DICTIONARY_ASSETS, _pyglossary_class, find_dictionary, read_mac_dictionary

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--supplements", type=Path, required=True)
    parser.add_argument("--dictionary-root", type=Path, default=DICTIONARY_ASSETS)
    parser.add_argument("--include-traditional", action="store_true", help="Also match Dr. Eye and convert its Chinese characters with macOS ICU")
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    sources = {w["word"].casefold(): {"word": w["word"]} for w in catalog["words"]}
    with tempfile.TemporaryDirectory(prefix="chinese-dictionary-supplements-") as folder:
        glossary = _pyglossary_class(Path(folder))
        for language, name in (("zh", "Simplified Chinese - English.dictionary"),
                               ("en", "New Oxford American Dictionary.dictionary")):
            read_mac_dictionary(find_dictionary(args.dictionary_root, name), sources, language, glossary)
    document = json.loads(args.supplements.read_text(encoding="utf-8")) if args.supplements.exists() else {"format_version": 1, "entries": []}
    existing = {(e["word"], e["sense_key"]) for e in document["entries"]}
    additions = [e for e in collect(catalog, sources) if (e["word"], e["sense_key"]) not in existing]
    if args.include_traditional:
        from build_vocab_bundle import normalize
        from traditional_dictionary_senses import parse_traditional_record, simplify_chinese
        traditional = {k: {"mac_en": v.get("mac_en", {}), "mac_zh": {"senses": []}} for k, v in sources.items()}
        with tempfile.TemporaryDirectory(prefix="traditional-dictionary-supplements-") as folder:
            glossary = _pyglossary_class(Path(folder))()
            glossary.read(str(find_dictionary(args.dictionary_root, "Traditional Chinese - English.dictionary")),
                          formatName="AppleDictBin", direct=True, progressbar=False, html=True)
            try:
                for record in glossary:
                    aliases = re.split(r"\s*[|;]\s*", str(record.s_term))
                    key = normalize(aliases[0])
                    if key in traditional:
                        traditional[key]["mac_zh"]["senses"].extend(parse_traditional_record(record.defi, key, aliases))
            finally:
                glossary.cleanup()
        claimed = existing | {(e["word"], e["sense_key"]) for e in additions}
        recovered = [e for e in collect(catalog, traditional, "macOS Traditional Chinese - English (Dr. Eye)")
                     if (e["word"], e["sense_key"]) not in claimed]
        converted = simplify_chinese([e["definition_cn"] for e in recovered])
        for entry, gloss in zip(recovered, converted):
            entry["dictionary_definition_cn_original"] = entry["definition_cn"]
            entry["definition_cn"] = gloss
        additions.extend(recovered)
    additions = exclude_known_mismatches(additions)
    document["entries"].extend(additions)
    args.supplements.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Added {len(additions):,} dictionary glosses; {len(document['entries']):,} total supplements")


if __name__ == "__main__":
    main()
