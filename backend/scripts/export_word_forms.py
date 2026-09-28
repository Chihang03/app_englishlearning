"""Export dictionary-attested spelling hints without replacing learning sentences."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile

from lxml import html

from dictionary_senses import FORM_LABELS, inflections_from_root
from build_vocab_bundle import DICTIONARY_ASSETS, _pyglossary_class, find_dictionary


def write_word_forms(entries: dict, output: Path) -> None:
    words = {}
    for word, entry in entries.items():
        combined = {}
        for bucket in ("mac_en", "mac_zh"):
            for family, forms in entry.get(bucket, {}).get("inflections", {}).items():
                for form, kinds in forms.items():
                    saved = combined.setdefault(family, {}).setdefault(form, [])
                    saved[:] = [kind for kind in FORM_LABELS if kind in {*saved, *kinds}]
        if combined:
            words[word] = combined
    output.write_text(json.dumps({"format_version": 1, "source": "macOS Dictionary", "words": words},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dictionary-root", type=Path, default=DICTIONARY_ASSETS)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1]/"data"/"word_forms.json")
    args = parser.parse_args()
    data = Path(__file__).resolve().parents[1]/"data"
    catalog = json.loads((data/"vocabulary_catalog.json").read_text())
    entries = {w["word"].casefold(): {} for w in catalog["words"]}
    entries.update({w["word"].casefold(): {} for w in json.loads((data/"seed_words.json").read_text())})
    with tempfile.TemporaryDirectory(prefix="word-forms-export-") as folder:
        Glossary = _pyglossary_class(Path(folder))
        for language, name in (("en", "New Oxford American Dictionary.dictionary"),
                               ("zh", "Simplified Chinese - English.dictionary")):
            glossary = Glossary()
            glossary.read(str(find_dictionary(args.dictionary_root,name)),formatName="AppleDictBin",
                          direct=True,progressbar=False,html=True)
            try:
                for record in glossary:
                    aliases = re.split(r"\s*[|;]\s*",str(record.s_term))
                    word = aliases[0].strip().casefold()
                    if word not in entries:
                        continue
                    root = html.fromstring(record.defi)
                    forms = inflections_from_root(root,word,aliases)
                    bucket = entries[word].setdefault("mac_"+language, {}).setdefault("inflections", {})
                    for family, values in forms.items():
                        for form, kinds in values.items():
                            saved = bucket.setdefault(family, {}).setdefault(form, [])
                            saved[:] = sorted(set(saved + kinds))
            finally:
                glossary.cleanup()
    write_word_forms(entries,args.output)
    print(f"Exported dictionary spelling hints to {args.output}")


if __name__ == "__main__":
    main()
