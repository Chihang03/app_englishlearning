"""Keep the dictionary's sense/part-of-speech/example relationships intact.

Only this export module needs lxml or access to a Mac dictionary. The resulting
JSON is sufficient for every server-side operation.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from lxml import html


POS = {
    "noun": "名词", "verb": "动词", "transitive verb": "及物动词",
    "intransitive verb": "不及物动词", "reflexive verb": "反身动词",
    "adjective": "形容词", "adverb": "副词", "preposition": "介词",
    "conjunction": "连词", "pronoun": "代词", "determiner": "限定词",
    "article": "冠词", "numeral": "数词", "interjection": "感叹词",
    "auxiliary verb": "助动词", "modal verb": "情态动词", "abbreviation": "缩写",
    "exclamation": "感叹词",
}
IRREGULAR = {
    "be": "am is are was were been being", "have": "has had", "do": "does did done",
    "go": "went gone", "come": "came", "get": "got gotten", "give": "gave given",
    "take": "took taken", "make": "made", "say": "said", "see": "saw seen",
    "know": "knew known", "think": "thought", "find": "found", "leave": "left",
    "feel": "felt", "tell": "told", "keep": "kept", "meet": "met", "run": "ran",
    "write": "wrote written", "speak": "spoke spoken", "break": "broke broken",
    "choose": "chose chosen", "begin": "began begun", "drink": "drank drunk",
    "drive": "drove driven", "eat": "ate eaten", "fall": "fell fallen",
    "fly": "flew flown", "forget": "forgot forgotten", "grow": "grew grown",
    "hold": "held", "hear": "heard", "lose": "lost", "pay": "paid",
    "read": "read", "sell": "sold", "send": "sent", "sit": "sat",
    "sleep": "slept", "stand": "stood", "teach": "taught", "understand": "understood",
    "win": "won", "bring": "brought", "buy": "bought", "catch": "caught",
    "fight": "fought", "build": "built", "draw": "drew drawn", "wear": "wore worn",
    "rise": "rose risen", "swim": "swam swum", "throw": "threw thrown",
    "man": "men", "woman": "women", "child": "children", "person": "people",
    "foot": "feet", "tooth": "teeth", "mouse": "mice", "goose": "geese",
    "good": "better best", "bad": "worse worst", "well": "better best",
    "light": "lit",
}


def text(node: Any) -> str:
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def has(node: Any, name: str) -> bool:
    return name in (node.get("class") or "").split()


def nodes(node: Any, name: str) -> list[Any]:
    return node.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),$cls)]", cls=f" {name} ")


def ancestor(node: Any, classes: tuple[str, ...]) -> Any:
    return next((p for p in node.iterancestors() if any(has(p, c) for c in classes)), None)


def owned(group: Any, name: str, groups: tuple[str, ...]) -> list[Any]:
    return [n for n in nodes(group, name) if ancestor(n, groups) is group]


def allowed_forms(word: str, aliases: list[str], pos: str) -> set[str]:
    """Accept only inflections attested in this dictionary record, not derivatives."""
    word = word.casefold()
    regular = {word, word + "s", word + "es"}
    if word.endswith("y"):
        regular.add(word[:-1] + "ies")
    if "verb" in pos:
        regular.update({word + "ed", word + "d", word + "ing", word[:-1] + "ing"})
        if word.endswith("y"):
            regular.add(word[:-1] + "ied")
        if word[-1:] and word[-1] not in "aeiouwxy":
            regular.update({word + word[-1] + "ed", word + word[-1] + "ing"})
    if pos in ("adjective", "adverb"):
        regular.update({word + "er", word + "est", word + "r", word + "st",
                        word[:-1] + "ier", word[:-1] + "iest"})
    regular.update(IRREGULAR.get(word, "").split())
    return {word} | regular.intersection(a.casefold() for a in aliases)


def example(sentence: str, word: str, aliases: list[str], pos: str) -> dict[str, str] | None:
    sentence = re.sub(r"\s+", " ", sentence).strip().rstrip(" |;,")
    if re.search(r"\b(?:sb|sth|sbd|sthg)\b|…|\.\.\.", sentence, re.I):
        return None
    if re.match(r"^to\s+", sentence, re.I):
        return None  # dictionary construction templates are not sentences
    if len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", sentence)) < 4:
        return None
    for form in sorted(allowed_forms(word, aliases, pos), key=lambda f: (f != word.casefold(), f)):
        for match in re.finditer(rf"(?<![A-Za-z'-]){re.escape(form)}(?![A-Za-z'-])", sentence, re.I):
            prefix = sentence[:match.start()]
            if match.group()[:1].isupper() and form != "i" and prefix.strip():
                last_break = max(prefix.rfind("."), prefix.rfind("!"), prefix.rfind("?"))
                if prefix[last_break + 1:].strip():
                    continue
            # Bilingual dictionary examples omit terminal punctuation in their
            # HTML. Add punctuation without inventing or changing sentence text.
            if not re.search(r"[.!?][\"')’”\]]*$", sentence):
                sentence += "."
            return {"sentence": sentence, "target_form": form}
    return None


def parse_record(definition_html: str, word: str, language: str, aliases: list[str]) -> dict[str, Any]:
    try:
        root = html.fromstring(definition_html)
    except (TypeError, ValueError):
        return {}
    result: dict[str, Any] = {"senses": []}
    phones = nodes(root, "ph")
    ipa = [p for p in phones if has(p, "t_IPA")] or phones
    if ipa:
        preferred = sorted(ipa, key=lambda p: (p.get("dialect", "") != "AmE", len(text(p))))[0]
        result["pronunciation"] = text(preferred)

    classes = ("semb",) if language == "zh" else ("msDict",)
    for group in nodes(root, classes[0]):
        block = ancestor(group, ("gramb",) if language == "zh" else ("se1",))
        if block is None or ancestor(group, ("subEntry",)) is not None:
            continue
        # Reflexive constructions/phrasal verbs deserve their own headwords;
        # their meanings cannot be assigned to the bare target word.
        # Forms inside a meaning group also describe plurals or variants. Only
        # a form on the grammatical block identifies a separate construction.
        phrases = [n for n in owned(block, "frm", ("gramb",))
                   if ancestor(n, ("semb",)) is None] if language == "zh" else []
        if phrases and text(phrases[0]).removeprefix("to ").casefold() != word.casefold():
            continue
        ps = owned(block, "ps" if language == "zh" else "pos", ("gramb", "se1"))
        pos = text(ps[0]).casefold() if ps else ""
        cn = ""
        en = ""
        if language == "zh":
            translations = [text(n) for n in owned(group,"trans",classes)
                            if not has(n,"ty_pinyin") and ancestor(n,("exg",)) is None]
            cn = "、".join(dict.fromkeys(v for v in translations if v))
            indicators = [text(n).strip("() ") for n in owned(group,"ind",classes)
                          if ancestor(n,("exg",)) is None]
            en = "; ".join(dict.fromkeys(indicators))
        else:
            defs = owned(group, "df", ("msDict",))
            en = "; ".join(dict.fromkeys(text(n).rstrip(" :") for n in defs if text(n)))
        if not cn and not en:
            continue
        sense_id = group.get("lexid") or group.get("id") or hashlib.sha256(
            f"{word}|{pos}|{cn}|{en}".encode()).hexdigest()[:24]
        examples = []
        for eg in owned(group, "exg" if language == "zh" else "eg", classes):
            ex = nodes(eg, "ex")
            if not ex or nodes(ex[0], "underline"):
                continue
            item = example(text(ex[0]), word, aliases, pos)
            if not item:
                continue
            translation = ""
            if language == "zh":
                translated = [text(n) for n in nodes(eg, "trans") if not has(n, "ty_pinyin")]
                translation = next(iter(translated), "")
            item.update({"translation_cn": translation or None, "source": "macOS Dictionary"})
            if not any(e["sentence"] == item["sentence"] for e in examples):
                examples.append(item)
        result["senses"].append({
            "key": f"{language}:{sense_id}", "part_of_speech": POS.get(pos, pos or "词汇"),
            "definition_cn": cn, "definition_en": en or None,
            "source": "macOS Dictionary", "examples": examples,
        })
    return result
