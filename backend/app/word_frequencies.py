"""Offline headword frequencies, separate from dictionary IDs and user progress."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .vocabulary_json import json_fingerprint


WORD_FREQUENCIES_PATH = Path(__file__).resolve().parents[1] / 'data' / 'word_frequencies.json'


def normalize_headword(word: str) -> str:
    return word.strip().casefold().replace('’', "'")


def frequency_tie_break(word: str) -> str:
    # A stable shuffle within each frequency bin, shared across lists and devices.
    return hashlib.sha256(('word-frequency-order-v1:' + word).encode('utf-8')).hexdigest()


def sync_word_frequencies(conn, path: Path = WORD_FREQUENCIES_PATH) -> bool:
    """Replace only frequency metadata when its standalone snapshot changes.

    Missing files keep any previously imported snapshot. Unknown frequencies are
    NULL, never a measured zero. Integer hundredths avoid floating-point ties.
    """
    if not path.exists():
        return False
    document = path.read_bytes()
    fingerprint = json_fingerprint(document)
    stored = conn.execute("SELECT value FROM word_frequency_state WHERE key='fingerprint'").fetchone()
    if stored and stored[0] == fingerprint:
        return False
    payload = json.loads(document)
    source = payload.get('source', {})
    if (payload.get('format_version') != 1 or source.get('language') != 'en'
            or source.get('score_scale') != 100 or not source.get('version')):
        raise ValueError('Invalid English word-frequency snapshot metadata')
    words = payload.get('words')
    if not isinstance(words, dict) or not words:
        raise ValueError('Word-frequency snapshot must contain headwords')
    rows = []
    for word, score in words.items():
        if not word or word != normalize_headword(word):
            raise ValueError(f'Invalid frequency headword: {word!r}')
        if score is not None and (type(score) is not int or not 1 <= score <= 900):
            raise ValueError(f'Invalid Zipf frequency for {word!r}: {score!r}')
        rows.append((word, score, frequency_tie_break(word)))
    # Validate the complete snapshot before replacing anything. The caller owns
    # the transaction, just as it does for other startup content synchronizers.
    conn.execute('DELETE FROM word_frequencies')
    conn.executemany('INSERT INTO word_frequencies(headword,zipf_cent,tie_break) VALUES(?,?,?)', rows)
    conn.executemany('INSERT OR REPLACE INTO word_frequency_state(key,value) VALUES(?,?)', [
        ('fingerprint', fingerprint),
        ('source', json.dumps(source, ensure_ascii=False)),
    ])
    return True
