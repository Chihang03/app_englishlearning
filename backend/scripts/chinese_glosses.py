"""Apply checked, sense-specific Chinese supplements without changing sense IDs."""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_SUPPLEMENTS = Path(__file__).resolve().parents[1] / "data" / "chinese_gloss_supplements.json"


def apply_supplements(words: list[dict], path: Path = DEFAULT_SUPPLEMENTS) -> int:
    if not path.exists():
        return 0
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format_version") != 1:
        raise ValueError("Unsupported Chinese supplement format")
    supplements = {}
    for entry in document["entries"]:
        key = (entry["word"], entry["sense_key"])
        if key in supplements:
            raise ValueError(f"Duplicate Chinese supplement: {key}")
        if not re.search(r"[\u3400-\u9fff]", entry["definition_cn"]):
            raise ValueError(f"Chinese supplement has no Chinese text: {key}")
        supplements[key] = entry
    applied = 0
    for word in words:
        for sense in word["senses"]:
            entry = supplements.get((word["word"], sense["key"]))
            if not entry or str(sense.get("definition_cn") or "").strip():
                continue
            if sense.get("definition_en") != entry["expected_definition_en"] or sense["part_of_speech"] != entry["expected_part_of_speech"]:
                raise ValueError(f"Sense changed; review Chinese supplement: {word['word']} {sense['key']}")
            sense["definition_cn"] = entry["definition_cn"].strip()
            applied += 1
        # The flat dictionary entry describes the first sense, too.
        if word["senses"]:
            word["definition_cn"] = word["senses"][0]["definition_cn"]
    return applied
