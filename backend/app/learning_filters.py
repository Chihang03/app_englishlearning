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


def basic_word_sql() -> str:
    """Match the words alias w by spelling, across lists and duplicate rows."""
    return f"lower(trim(w.word)) IN ({_BASIC_WORDS_SQL})"


def learnable_word_sql(user_expression: str) -> str:
    """Only internal SQL expressions are accepted; callers use words alias w."""
    return f"""({unmuted_sql(user_expression)} AND NOT (
        EXISTS(SELECT 1 FROM settings basic_setting
            WHERE basic_setting.user_id={user_expression}
              AND basic_setting.key='skip_basic_600' AND basic_setting.value='true')
        AND {basic_word_sql()}))"""
