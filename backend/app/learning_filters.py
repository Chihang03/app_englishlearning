from __future__ import annotations

import json
from pathlib import Path

from .muted_words import unmuted_sql


# The source snapshot includes all 600 heads, even words that currently lack a
# usable example in the catalog. This also covers later/private imports.
_SOURCE_PATH = Path(__file__).resolve().parents[1] / "data" / "source_word_lists.json"
_lists = json.loads(_SOURCE_PATH.read_text(encoding="utf-8"))["lists"]
BASIC_WORDS = frozenset(
    word.strip().lower()
    for word in next(item["words"] for item in _lists if item["id"] == "ngsl_core")[:600]
)
_BASIC_WORDS_SQL = ",".join("'" + word.replace("'", "''") + "'" for word in sorted(BASIC_WORDS))


# NGSL lists these grammatical families under their headword. Exam lists can
# also contain the object, possessive, reflexive or plural forms as new entries.
# This is an explicit grammar mapping, not a stemmer or a derivation rule.
_GRAMMATICAL_FAMILIES = {
    "i": ("me", "my", "mine", "myself"),
    "he": ("him", "his", "himself"),
    "she": ("her", "hers", "herself"),
    "we": ("us", "our", "ours", "ourselves"),
    "they": ("them", "their", "theirs", "themself", "themselves"),
    "you": ("your", "yours", "yourself", "yourselves"),
    "it": ("its", "itself"),
    "who": ("whom", "whose"),
    "this": ("these",),
    "that": ("those",),
    "one": ("oneself",),
}
BASIC_WORD_FORMS = frozenset(
    form for head, forms in _GRAMMATICAL_FAMILIES.items() if head in BASIC_WORDS
    for form in forms
)
_BASIC_FORMS_SQL = ",".join("'" + form + "'" for form in sorted(BASIC_WORD_FORMS))
_GRAMMATICAL_POS_SQL = ",".join("'" + pos + "'" for pos in (
    "代词", "限定词", "pronoun", "personal pronoun", "possessive pronoun",
    "reflexive pronoun", "relative pronoun", "demonstrative pronoun",
    "determiner", "possessive determiner", "demonstrative determiner",
))


def basic_word_sql(*, unit_expression: str | None = None) -> str:
    """Match words alias w, preserving independent senses at a form's spelling.

    Unit queries supply their unit ID. Word-level legacy/progress queries only
    exclude a grammatical form when every usable sense is grammatical.
    """
    grammatical = f"""(lower(trim(basic_sense.part_of_speech)) IN ({_GRAMMATICAL_POS_SQL})
        OR (lower(trim(w.word)) IN ('whose','these','those')
            AND lower(trim(basic_sense.part_of_speech)) IN ('形容词','adjective'))
        OR (lower(trim(w.word))!='mine'
            AND lower(trim(basic_sense.part_of_speech))='词汇'))"""
    scope = """basic_sense.word_id=w.id AND basic_sense.active=1
        AND basic_sense.learning_enabled=1 AND EXISTS (
            SELECT 1 FROM sense_examples basic_example
            WHERE basic_example.sense_id=basic_sense.id AND basic_example.active=1)"""
    if unit_expression is not None:
        scope += f""" AND EXISTS (SELECT 1 FROM learning_unit_senses basic_binding
            WHERE basic_binding.sense_id=basic_sense.id
              AND basic_binding.learning_unit_id={unit_expression})"""
    return f"""(lower(trim(w.word)) IN ({_BASIC_WORDS_SQL}) OR (
        lower(trim(w.word)) IN ({_BASIC_FORMS_SQL})
        AND EXISTS (SELECT 1 FROM word_senses basic_sense WHERE {scope} AND {grammatical})
        AND NOT EXISTS (SELECT 1 FROM word_senses basic_sense WHERE {scope} AND NOT {grammatical})))"""


def learnable_word_sql(user_expression: str, *, unit_expression: str | None = None) -> str:
    """Only internal SQL expressions are accepted; callers use words alias w."""
    return f"""({unmuted_sql(user_expression)} AND NOT (
        EXISTS(SELECT 1 FROM settings basic_setting
            WHERE basic_setting.user_id={user_expression}
              AND basic_setting.key='skip_basic_600' AND basic_setting.value='true')
        AND {basic_word_sql(unit_expression=unit_expression)}))"""
