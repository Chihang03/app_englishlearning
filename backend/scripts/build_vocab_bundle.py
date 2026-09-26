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
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]*$")
TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
POS_MAP = {
    "n": "名词", "noun": "名词", "v": "动词", "verb": "动词",
    "vt": "及物动词", "vi": "不及物动词", "adj": "形容词",
    "adjective": "形容词", "a": "形容词", "adv": "副词",
    "adverb": "副词", "r": "副词", "prep": "介词",
    "preposition": "介词", "conj": "连词", "conjunction": "连词",
    "pron": "代词", "pronoun": "代词", "art": "冠词",
    "article": "冠词", "num": "数词", "numeral": "数词",
    "int": "感叹词", "interjection": "感叹词", "abbr": "缩写",
    "determiner": "限定词", "auxiliary verb": "助动词",
}
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


def iter_tar_member(path: Path, member_name: str) -> Iterable[str]:
    with tarfile.open(path, mode="r:bz2") as archive:
        member = archive.getmember(member_name)
        stream = archive.extractfile(member)
        if stream is None:
            return
        for raw_line in stream:
            yield raw_line.decode("utf-8", errors="replace").rstrip("\r\n")


def contains_headword(sentence: str, word: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z'-]){re.escape(word)}(?![A-Za-z'-])", re.IGNORECASE)
    for match in pattern.finditer(sentence):
        matched = match.group(0)
        if matched == word or word.casefold() == "i":
            return True
        if matched[:1].isupper():
            prefix = sentence[:match.start()]
            last_sentence_break = max(prefix.rfind("."), prefix.rfind("!"), prefix.rfind("?"))
            if not prefix[last_sentence_break + 1:].strip():
                return True
    return False


def is_example_sentence(sentence: str, word: str) -> bool:
    sentence = clean_text(sentence)
    return (
        len(TOKEN_RE.findall(sentence)) >= 4
        and bool(re.search(r"[.!?][\"')\]]*$", sentence))
        and contains_headword(sentence, word)
    )


def read_tatoeba_cc0(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Keep only real CC0 English sentences as a fallback for dictionary gaps."""
    by_word: dict[str, list[str]] = defaultdict(list)
    targets = set(entries)
    for line in iter_tar_member(path, "sentences_CC0.csv"):
        columns = line.split("\t", 3)
        if len(columns) < 3 or columns[1] != "eng":
            continue
        sentence = clean_text(columns[2])
        if not 18 <= len(sentence) <= 180:
            continue
        matched = {normalize(token) for token in TOKEN_RE.findall(sentence)}
        for word in matched.intersection(targets):
            if is_example_sentence(sentence, word) and len(by_word[word]) < 12:
                by_word[word].append(sentence)

    for word, sentences in by_word.items():
        sentences.sort(key=lambda sentence: (abs(len(sentence) - 75), len(sentence)))
        if sentences:
            entries[word]["fallback_example"] = sentences[0]
            entries[word]["fallback_example_source"] = "Tatoeba CC0 1.0"


def read_wordnet_examples(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Use WordNet examples only when the Mac dictionaries have no sentence."""
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive:
            if not member.name.startswith("dict/data.") or not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            for raw_line in stream:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line or line.startswith("  ") or "|" not in line:
                    continue
                header, gloss = line.split("|", 1)
                fields = header.split()
                if len(fields) < 5:
                    continue
                try:
                    word_count = int(fields[3], 16)
                except ValueError:
                    continue
                lemmas = [fields[4 + 2 * i].replace("_", " ").casefold() for i in range(word_count)]
                examples = re.findall(r'"([^\"]+)"', gloss)
                for lemma in lemmas:
                    key = normalize(lemma)
                    entry = entries.get(key)
                    if not entry or entry.get("fallback_example"):
                        continue
                    for example in examples:
                        if is_example_sentence(example, key):
                            entry["fallback_example"] = clean_text(example)
                            entry["fallback_example_source"] = "Princeton WordNet 3.0"
                            break


def read_fallback_snapshot(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    for word, value in payload.items():
        entry = entries.get(normalize(word))
        if entry and isinstance(value, dict):
            sentence = clean_text(value.get("sentence"))
            if is_example_sentence(sentence, entry["word"]):
                entry["fallback_example"] = sentence
                entry["fallback_example_source"] = clean_text(value.get("source")) or "本地备用例句"
def clean_text(value: str | None, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def _class_nodes(root: Any, class_name: str) -> list[Any]:
    return root.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), $class_name)]",
        class_name=f" {class_name} ",
    )


def _has_class(node: Any, class_name: str) -> bool:
    return class_name in (node.get("class") or "").split()


def _node_text(node: Any) -> str:
    return clean_text("".join(node.itertext()))


def parse_mac_record(definition_html: str, word: str, language: str) -> dict[str, Any]:
    from lxml import html

    try:
        root = html.fromstring(definition_html)
    except (TypeError, ValueError):
        return {}

    result: dict[str, Any] = {}
    pos_classes = ("ps", "pos")
    for class_name in pos_classes:
        nodes = _class_nodes(root, class_name)
        if nodes:
            result["part_of_speech"] = POS_MAP.get(_node_text(nodes[0]).casefold(), _node_text(nodes[0]))
            break

    pronunciations = _class_nodes(root, "ph")
    if pronunciations:
        def pronunciation_rank(node: Any) -> tuple[int, int]:
            parent = node.getparent()
            dialect = parent.get("dialect", "") if parent is not None else ""
            return (0 if dialect == "AmE" else 1, len(_node_text(node)))

        preferred = sorted(pronunciations, key=pronunciation_rank)[0]
        result["pronunciation"] = _node_text(preferred)

    if language == "zh":
        meanings: list[str] = []
        for meaning_group in _class_nodes(root, "semb"):
            for translation_group in meaning_group:
                if not _has_class(translation_group, "trg"):
                    continue
                for translation in _class_nodes(translation_group, "trans"):
                    if _has_class(translation, "ty_pinyin"):
                        continue
                    value = _node_text(translation)
                    if value and value not in meanings:
                        meanings.append(value)
        if meanings:
            result["definition_cn"] = "；".join(meanings[:8])

        examples: list[dict[str, str]] = []
        for example_group in _class_nodes(root, "exg"):
            example_nodes = [node for node in _class_nodes(example_group, "ex") if node is not example_group]
            if not example_nodes:
                continue
            sentence = _node_text(example_nodes[0])
            if not is_example_sentence(sentence, word):
                continue
            translations = [
                _node_text(node) for node in _class_nodes(example_group, "trans")
                if not _has_class(node, "ty_pinyin")
            ]
            examples.append({
                "sentence": sentence,
                "translation": next((item for item in translations if item), ""),
            })
        if examples:
            result["examples"] = examples
    else:
        definitions = [_node_text(node) for node in _class_nodes(root, "df")]
        if definitions:
            result["definition_en"] = "; ".join(dict.fromkeys(item for item in definitions if item))
        examples = []
        for example_group in _class_nodes(root, "eg"):
            sentence = _node_text(example_group)
            if is_example_sentence(sentence, word):
                examples.append({"sentence": sentence, "translation": ""})
        if examples:
            result["examples"] = examples
    return result


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
                details = parse_mac_record(record.defi, key, language)
                bucket = entry.setdefault("mac_zh" if language == "zh" else "mac_en", {})
                for field, value in details.items():
                    if field == "examples":
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


def dictionary_fields(entry: dict[str, Any]) -> dict[str, Any]:
    ecdict = entry.get("ecdict") or {}
    mac_zh = entry.get("mac_zh") or {}
    mac_en = entry.get("mac_en") or {}

    translation = clean_text((ecdict.get("translation") or "").replace("\n", " / "), 500)
    english_definition = clean_text((ecdict.get("definition") or "").replace("\n", " / "), 1000)
    pos_key = ""
    pos_match = re.match(
        r"^\s*(vt|vi|adj|adv|prep|conj|pron|art|num|abbr|n|v|a|r)\.?(?=\s)",
        translation,
        re.I,
    )
    if pos_match:
        pos_key = pos_match.group(1).casefold()
    example = next(
        (item for item in mac_en.get("examples", []) if is_example_sentence(item.get("sentence", ""), entry["word"])),
        None,
    )
    if example is None:
        example = next(
            (item for item in mac_zh.get("examples", []) if is_example_sentence(item.get("sentence", ""), entry["word"])),
            None,
        )

    fallback_example = entry.get("fallback_example")
    if example:
        example_sentence = clean_text(example["sentence"])
        example_translation = clean_text(example.get("translation")) or None
        example_source = "macOS Dictionary"
    elif fallback_example and is_example_sentence(fallback_example, entry["word"]):
        example_sentence = clean_text(fallback_example)
        example_translation = None
        example_source = entry.get("fallback_example_source")
    else:
        example_sentence = ""
        example_translation = None
        example_source = None

    return {
        "part_of_speech": mac_zh.get("part_of_speech") or mac_en.get("part_of_speech") or POS_MAP.get(pos_key) or "词汇",
        "definition_cn": clean_text(mac_zh.get("definition_cn") or translation, 500) or "词义待补充",
        "definition_en": clean_text(mac_en.get("definition_en") or english_definition, 1000) or None,
        "pronunciation": clean_text(mac_zh.get("pronunciation") or mac_en.get("pronunciation") or ecdict.get("phonetic")) or entry["word"],
        "example_sentence": example_sentence,
        "example_translation_cn": example_translation,
        "example_source": example_source,
    }


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

    fallback_path = output.with_name("example_fallbacks.json")
    read_fallback_snapshot(fallback_path, entries)

    # Open fallback corpora first; Mac Dictionary examples take precedence.
    if source_dir is not None:
        cc0_path = source_dir / "tatoeba_sentences_CC0.tar.bz2"
        if cc0_path.exists():
            read_tatoeba_cc0(cc0_path, entries)
        wordnet_path = source_dir / "wordnet_db.tar.gz"
        if wordnet_path.exists():
            read_wordnet_examples(wordnet_path, entries)

    if english_dictionary is None:
        english_dictionary = find_dictionary(dictionary_root, "New Oxford American Dictionary.dictionary")
    if chinese_dictionary is None:
        chinese_dictionary = find_dictionary(dictionary_root, "Simplified Chinese - English.dictionary")

    with tempfile.TemporaryDirectory(prefix="vocab-dictionary-cache-") as cache:
        glossary_class = _pyglossary_class(Path(cache))
        read_mac_dictionary(chinese_dictionary, entries, "zh", glossary_class)
        read_mac_dictionary(english_dictionary, entries, "en", glossary_class)

    words: list[dict[str, Any]] = []
    missing: dict[str, list[str]] = defaultdict(list)
    used_fallbacks: dict[str, dict[str, str]] = {}
    for entry in entries.values():
        fields = dictionary_fields(entry)
        if not fields["example_sentence"]:
            for list_id in entry["packs"]:
                missing[list_id].append(entry["word"])
            continue
        if fields["example_source"] != "macOS Dictionary":
            used_fallbacks[entry["word"]] = {
                "sentence": fields["example_sentence"],
                "source": str(fields["example_source"] or ""),
            }
        words.append({
            "word": entry["word"],
            **fields,
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
    fallback_path.write_text(
        json.dumps(used_fallbacks, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"lists": list_metadata, "words": words}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(words):,} learnable words across {len(PACKS)} lists to {output}")
    print(f"Saved headword-only source lists to {output.with_name('source_word_lists.json')}")
    print(f"Saved {len(used_fallbacks):,} localized fallback examples to {fallback_path}")
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
