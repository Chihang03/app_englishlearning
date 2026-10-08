"""Conservative sense-level alignment of Oxford and Dr. Eye source records."""
from __future__ import annotations

from copy import deepcopy
import re

from app.word_forms import pos_family


def gloss_terms(value: str) -> set[str]:
    return {term.strip().strip("。 .") for term in re.split(r"[、；;，]", value) if term.strip()}


def sense_family(sense: dict) -> str:
    value = sense['part_of_speech']
    return pos_family(value) or value.strip().casefold()


def append_examples(destination: dict, source: dict, method: str) -> None:
    for original in source["examples"]:
        example = deepcopy(original)
        if source.get("provenance"):
            example["provenance"] = {**source["provenance"], "source_sense_key": source["key"],
                                     "alignment": method}
        previous = next((e for e in destination["examples"] if e["sentence"] == example["sentence"]), None)
        if previous is None:
            destination["examples"].append(example)


def align_senses(entry: dict, overrides: dict) -> tuple[list[dict], list[dict]]:
    chinese = {s["key"]: deepcopy(s) for s in entry.get("mac_zh", {}).get("senses", [])}
    english = {s["key"]: deepcopy(s) for s in entry.get("mac_en", {}).get("senses", [])}
    traditional = {s["key"]: deepcopy(s) for s in entry.get("mac_tw", {}).get("senses", [])}
    covered = set()
    unmatched = []
    for sense in english.values():
        override = overrides.get(sense["key"])
        destination = None
        if override and override["word"] == entry["word"]:
            if sense["definition_en"] != override["expected_definition_en"]:
                raise ValueError(f"Dictionary changed: review override {sense['key']}")
            destination = chinese.get(override.get("merge_into"))
            if override.get("merge_into") and destination is None:
                raise ValueError(f"Dictionary changed: missing destination {override['merge_into']}")
            sense["definition_cn"] = override["definition_cn"]
        elif not override:
            sentences = {e["sentence"].casefold() for e in sense["examples"]}
            matches = [s for s in chinese.values()
                       if sense_family(s) == sense_family(sense)
                       and sentences.intersection(e["sentence"].casefold() for e in s["examples"])]
            if len(matches) == 1:
                destination = matches[0]
        if destination is not None:
            destination["definition_en"] = destination.get("definition_en") or sense["definition_en"]
            append_examples(destination, sense, "reviewed_override" if override else "exact_example")
            covered.add(sense["key"])
        else:
            unmatched.append(sense)
    # English evidence supplements each missing sense, even if the Chinese
    # dictionary already supplies another learnable meaning of this headword.
    canonical = [*chinese.values(), *unmatched]
    initial_usable_families = {sense_family(s) for s in canonical if s["examples"]}
    matches = {}
    for key, sense in traditional.items():
        matches[key] = [s for s in canonical
                        if sense_family(s) == sense_family(sense)
                        and gloss_terms(s.get("definition_cn") or "").intersection(gloss_terms(sense["definition_cn"]))]
    for key, sense in traditional.items():
        destinations = matches[key]
        if len(destinations) == 1 and sum(destinations[0] in m for m in matches.values()) == 1:
            append_examples(destinations[0], sense, "unique_exact_chinese_gloss_and_pos")
            covered.add(key)
        elif not destinations and sense_family(sense) not in initial_usable_families:
            # An original bilingual sense is a valid fallback where neither
            # Oxford source has a usable sense of this POS. Keep its own key.
            canonical.append(sense)

    rejected_reverse = []
    for reverse in entry.get("mac_reverse", []):
        candidates = [s for s in canonical if reverse["definition_cn"] in gloss_terms(s.get("definition_cn") or "")
                      and (not reverse["pos_family"] or sense_family(s) == reverse["pos_family"])]
        if len(candidates) == 1:
            append_examples(candidates[0], reverse, "unique_exact_reverse_headword_and_gloss")
        else:
            rejected_reverse.append({"word": entry["word"], "key": reverse["key"],
                                     "definition_cn": reverse["definition_cn"],
                                     "reason": "ambiguous_reverse_alignment" if candidates else "unmatched_reverse_alignment",
                                     "candidate_sense_keys": [s["key"] for s in candidates],
                                     "provenance": reverse["provenance"]})

    usable = [s for s in canonical if s["examples"]]
    # Source ordering keeps a newly completed basic Chinese sense ahead of its
    # figurative subsenses, while historical source identities remain stable.
    for position, sense in enumerate(usable):
        sense["position"] = position
    gaps = []
    for sense in [*canonical, *(s for key, s in traditional.items() if key not in covered and s not in canonical)]:
        if sense not in usable:
            gaps.append({"word": entry["word"], "key": sense["key"],
                         "part_of_speech": sense["part_of_speech"],
                         "definition_cn": sense.get("definition_cn") or "",
                         "definition_en": sense.get("definition_en"),
                         "reason": "no_verified_alignment" if sense["examples"] else "missing_example"})
    return usable, [*gaps, *rejected_reverse]


def preserve_existing_senses(entry: dict, chosen: list[dict], previous: list[dict]) -> list[dict]:
    """New matching evidence must not retire a previously taught source key.

    Parser improvements can discover a Chinese counterpart for a historical
    English key. Keep the old answer and checked translations under that key,
    after verifying the original source still has the same meaning and POS.
    """
    originals = {s['key']: s for bucket in ('mac_zh', 'mac_en', 'mac_tw')
                 for s in entry.get(bucket, {}).get('senses', [])}
    old = {s['key']: deepcopy(s) for s in previous}
    for key, sense in old.items():
        original = originals.get(key)
        field = 'definition_en' if key.startswith('en:') or not (original or {}).get('definition_cn') else 'definition_cn'
        if original is None or sense_family(original) != sense_family(sense) or original.get(field) != sense.get(field):
            raise ValueError(f"Historical dictionary source changed: {entry['word']} {key}")
    result = []
    for current in chosen:
        retained = old.pop(current['key'], None)
        if retained is not None:
            append_examples(retained, current, 'same_source_sense')
            result.append(retained)
        else:
            result.append(current)
    result.extend(old.values())
    for position, sense in enumerate(result):
        sense['position'] = position
    return result
