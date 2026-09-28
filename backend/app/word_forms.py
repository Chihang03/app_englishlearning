"""Spelling hints from exported dictionary inflections, independent of tense parsing."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

from .ing_usage import ing_usage_label


WORD_FORMS_PATH = Path(__file__).resolve().parents[1] / "data" / "word_forms.json"
FORM_LABELS = {
    "first_person_singular": "第一人称单数",
    "third_person_singular": "第三人称单数",
    "second_person_or_plural": "第二人称／复数",
    "present": "现在式",
    "ing": "-ing 形式",
    "past": "过去式",
    "past_participle": "过去分词",
    "plural": "复数",
    "comparative": "比较级",
    "superlative": "最高级",
}


def pos_family(pos: str) -> str:
    for family, names in (("verb", ("动词", "verb")), ("pronoun", ("代词", "pronoun")),
                          ("numeral", ("数词", "numeral", "cardinal number", "ordinal number")),
                          ("noun", ("名词", "noun")),
                          ("adjective", ("形容词", "adjective")), ("adverb", ("副词", "adverb"))):
        if any(name in pos.casefold() for name in names):
            return family
    return ""


def regular_form_types(word: str, form: str, family: str) -> list[str]:
    """Classify a known spelling; dictionary aliases attest it during export."""
    word, form = word.casefold(), form.casefold()
    if not word or word == form:
        return []
    s_forms = {word + "s", word + "es"}
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        s_forms.add(word[:-1] + "ies")
    if family in ("noun", "pronoun", "numeral", "verb") and form in s_forms:
        return ["third_person_singular" if family == "verb" else "plural"]
    if family == "verb":
        ing = {word + "ing"}
        past = {word + "ed"}
        if word.endswith("e"):
            past.add(word + "d")
            if not word.endswith("ee"):
                ing.add(word[:-1] + "ing")
        if word.endswith("ie"):
            ing.add(word[:-2] + "ying")
        if word.endswith("y") and word[-2:-1] not in "aeiou":
            past.add(word[:-1] + "ied")
        if word[-1] not in "aeiouwxy":
            ing.add(word + word[-1] + "ing")
            past.add(word + word[-1] + "ed")
        if form in ing:
            return ["ing"]
        if form in past:
            return ["past", "past_participle"]
    if family in ("adjective", "adverb"):
        for suffix, kind in (("er", "comparative"), ("est", "superlative")):
            forms = {word + suffix}
            if word.endswith("e"):
                forms.add(word + suffix[1:])
            if word.endswith("y"):
                forms.add(word[:-1] + "i" + suffix)
            if word[-1] not in "aeiouwxy":
                forms.add(word + word[-1] + suffix)
            if form in forms:
                return [kind]
    return []


@lru_cache(maxsize=1)
def dictionary_forms() -> dict:
    return json.loads(WORD_FORMS_PATH.read_text(encoding="utf-8"))["words"]


def answer_form_label(word: str, form: str, pos: str, sentence: str = "") -> str | None:
    if word.casefold() == form.casefold():
        return None
    family = pos_family(pos)
    types = dictionary_forms().get(word.casefold(), {}).get(family, {}).get(form.casefold())
    if types is None:
        types = regular_form_types(word, form, family)
    if types == ["ing"] and sentence:
        return ing_usage_label(sentence, form, word)
    return "／".join(FORM_LABELS[kind] for kind in types if kind in FORM_LABELS) or None
