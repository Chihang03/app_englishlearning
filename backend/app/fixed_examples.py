"""Repair old rotating cards once; normal study never re-ranks a saved sentence."""
from pathlib import Path

CHOICES_SQL = (Path(__file__).with_name('fixed_example_choices.sql')).read_text()


def normalize_fixed_examples(conn) -> dict:
    from .learning_units import _upsert_snapshot_row
    choices = conn.execute(CHOICES_SQL).fetchall()
    changed = sum(row['example_id'] != row['previous_example_id'] for row in choices)
    for row in choices:
        conn.execute('''UPDATE user_sense_examples SET retired_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
            WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL AND sense_id!=?''',
            (row['user_id'], row['learning_unit_id'], row['sense_id']))
        _upsert_snapshot_row(conn, 'user_sense_examples', {
            key: row[key] for key in ('user_id', 'sense_id', 'example_id', 'learning_unit_id')
        } | {'retired_at': None})
    units = conn.execute("UPDATE learning_units SET example_strategy='fixed' WHERE example_strategy='rotate'").rowcount
    return {'units_fixed': units, 'cards_checked': len(choices), 'cards_changed': changed}
