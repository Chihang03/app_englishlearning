from __future__ import annotations

import json
from pathlib import Path

from .muted_words import unmuted_sql


# Object, possessive, reflexive and plural forms of the basic pronouns. They are
# skipped only where the sense itself is grammatical, so an independent meaning
# at the same spelling (mine, the noun) stays learnable. This is an explicit
# grammar mapping, not a stemmer or a derivation rule.
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
_FAMILY_FORMS = frozenset(form for forms in _GRAMMATICAL_FAMILIES.values() for form in forms)


# Basic words are the headwords every learner already meets constantly: a
# wordfreq Zipf score of 4.9 or more, about 1,000 entries of the catalogue. Usage
# frequency covers function words, numerals, everyday nouns and common
# inflected spellings in every list, where a rank in one course list did not.
_FREQUENCY_PATH = Path(__file__).resolve().parents[1] / "data" / "word_frequencies.json"
BASIC_ZIPF_CENT = 490
BASIC_WORDS = frozenset(
    word.strip().lower()
    for word, zipf in json.loads(_FREQUENCY_PATH.read_text(encoding="utf-8"))["words"].items()
    if zipf is not None and zipf >= BASIC_ZIPF_CENT
) - _FAMILY_FORMS
# Closed sets stay complete even where a member is less frequent or is stored
# only as a form of its headword: be/have/do forms, irregular comparatives and
# the elementary numerals.
BASIC_SUPPLEMENT = frozenset((
    "am", "is", "are", "was", "were", "been", "has", "had", "does", "did", "done",
    "better", "best", "worse", "worst",
    "zero", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "hundred", "thousand", "million", "billion",
    "third", "fourth", "fifth",
))
_BASIC_WORDS_SQL = ",".join(
    "'" + word.replace("'", "''") + "'" for word in sorted(BASIC_WORDS | BASIC_SUPPLEMENT))


BASIC_WORD_FORMS = frozenset(
    form for head, forms in _GRAMMATICAL_FAMILIES.items() if head in BASIC_WORDS
    for form in forms
)
_BASIC_FORMS_SQL = ",".join("'" + form + "'" for form in sorted(BASIC_WORD_FORMS))
_GRAMMATICAL_POS_SQL = ",".join("'" + pos + "'" for pos in (
    "代词", "限定词", "冠词", "article", "pronoun", "personal pronoun", "possessive pronoun",
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
    # Keep the original preference key for saved settings and open older clients.
    marker = f"/* LEVEL_UNIT {unit_expression} */" if unit_expression else "/* LEVEL_WORD w.id */"
    return f"""({unmuted_sql(user_expression)} AND NOT (
        EXISTS(SELECT 1 FROM settings basic_setting
            WHERE basic_setting.user_id={user_expression}
              AND basic_setting.key='skip_basic_600' AND basic_setting.value='true')
        AND {basic_word_sql(unit_expression=unit_expression)}) {marker})"""
