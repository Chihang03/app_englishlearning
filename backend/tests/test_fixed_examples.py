"""Stable examples across semantic merges, historical repair and native study."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest

from app import database, learning_units
from app.fixed_examples import normalize_fixed_examples
from app.senses import save_senses
import test_senses as fixtures

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('ENGLISH_TEST_NATIVE_CATALOG', ROOT/'rust/target/debug/englishctl'))


class FixedExampleTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review

    def prepare(self):
        with database.connect() as conn:
            word = conn.execute("SELECT * FROM words WHERE word='address'").fetchone()
            save_senses(conn, word['id'], [
                fixtures.sense('base', '处理', 'We address the problem.', pos='动词'),
                fixtures.sense('past', '处理', 'The problem was addressed.', target='addressed', pos='动词'),
            ])
            members = conn.execute('SELECT * FROM word_senses WHERE word_id=? AND active=1 ORDER BY position', (word['id'],)).fetchall()
            unit = learning_units._merge_group(conn, word, {
                'key': 'semantic:test', 'definition_cn': '处理', 'provenance': {'test': True},
                'example_strategy': 'rotate',
            }, members, 'semantic:test')
            conn.execute('INSERT OR IGNORE INTO word_list_learning_units(list_id,learning_unit_id,position,verification) SELECT list_id,?,position,\'reviewed\' FROM word_list_memberships WHERE word_id=?', (unit, word['id']))
            examples = conn.execute('SELECT e.* FROM learning_unit_senses b JOIN sense_examples e ON e.sense_id=b.sense_id WHERE b.learning_unit_id=? ORDER BY e.id', (unit,)).fetchall()
            return unit, [dict(e) for e in examples]

    def rotating_card(self):
        unit, examples = self.prepare()
        first = self.next()
        self.review(first, first['answer_form'])
        with database.connect() as conn:
            conn.execute("UPDATE learning_units SET example_strategy='rotate' WHERE id=?", (unit,))
            conn.execute('UPDATE user_sense_examples SET example_id=? WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL',
                         (examples[1]['id'], self.uid, unit))
        return unit, examples, first

    def test_old_rotate_flag_cannot_switch_base_example_on_repeated_due_reviews(self):
        unit, examples = self.prepare()
        with database.connect() as conn:
            conn.execute("UPDATE learning_units SET example_strategy='rotate' WHERE id=?", (unit,))
        first = self.next()
        self.assertEqual(first['answer_form'], 'address')
        for _ in range(4):
            self.assertEqual(self.next()['attempt_id'], first['attempt_id'])
            self.assertEqual(self.review(first, first['answer_form']).status_code, 200)
            with database.connect() as conn:
                conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=? AND learning_unit_id=?", (self.uid, unit))
                conn.execute("UPDATE review_history SET review_time='2000-01-01T00:00:00+00:00' WHERE user_id=?", (self.uid,))
            card = self.next()
            self.assertEqual(card['example_id'], examples[0]['id'])
            first = card

    def test_repair_optimizes_old_rotating_cards_once_without_altering_progress(self):
        unit, examples, first = self.rotating_card()
        with database.connect() as conn:
            tables = ('sense_srs_state', 'adaptive_memory', 'review_history', 'relearning_queue', 'study_attempts')
            before = {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] for table in tables}
            result = normalize_fixed_examples(conn)
            self.assertEqual(result, {'units_fixed': 1, 'cards_checked': 1, 'cards_changed': 1})
            self.assertEqual(conn.execute('SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL', (self.uid, unit)).fetchone()[0], examples[0]['id'])
            self.assertEqual(before, {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] for table in tables})
            self.assertEqual(normalize_fixed_examples(conn), {'units_fixed': 0, 'cards_checked': 0, 'cards_changed': 0})
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_repair_keeps_issued_round_and_replaces_only_future_example(self):
        unit, examples, first = self.rotating_card()
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=?", (self.uid,))
            conn.execute("UPDATE review_history SET review_time='2000-01-01T00:00:00+00:00' WHERE user_id=?", (self.uid,))
        active = self.next()
        self.assertEqual(active['answer_form'], 'addressed')
        with database.connect() as conn:
            normalize_fixed_examples(conn)
        self.assertEqual(self.next(), active)
        self.assertEqual(self.review(active, active['answer_form']).status_code, 200)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=?", (self.uid,))
        self.assertEqual(self.next()['example_id'], examples[0]['id'])

    def test_no_base_repair_restores_earliest_usable_encounter(self):
        unit, examples, first = self.rotating_card()
        with database.connect() as conn:
            conn.execute("UPDATE sense_examples SET sentence='They addressed the issue.',target_form='addressed' WHERE id=?", (examples[0]['id'],))
            normalize_fixed_examples(conn)
            chosen = conn.execute('SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL', (self.uid, unit)).fetchone()[0]
            self.assertEqual(chosen, first['example_id'])

    def test_repair_does_not_reselect_already_fixed_cards_or_archived_examples(self):
        unit, examples, first = self.rotating_card()
        with database.connect() as conn:
            conn.execute('UPDATE sense_examples SET active=0 WHERE id=?', (examples[0]['id'],))
            normalize_fixed_examples(conn)
            chosen = conn.execute('SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL', (self.uid, unit)).fetchone()[0]
            self.assertEqual(chosen, examples[1]['id'])
            conn.execute('UPDATE sense_examples SET active=1 WHERE id=?', (examples[0]['id'],))
            self.assertEqual(normalize_fixed_examples(conn)['cards_changed'], 0)
            self.assertEqual(learning_units.example_for_unit(conn, unit, self.uid)['id'], examples[1]['id'])

    def test_merge_inherits_first_encounter_instead_of_earliest_due_curve(self):
        first = self.next()
        self.review(first, first['answer_form'])
        second = self.next()
        self.review(second, second['answer_form'])
        with database.connect() as conn:
            word = conn.execute("SELECT * FROM words WHERE word='address'").fetchone()
            # Historical source curves can have different due dates. Their
            # ordering must not choose the surviving teaching sentence.
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE sense_id=?", (second['sense_id'],))
            members = conn.execute('SELECT * FROM word_senses WHERE word_id=? AND active=1 ORDER BY position', (word['id'],)).fetchall()
            # A reviewed merge requires one lexical identity; fixture meanings
            # are deliberately assigned one here to isolate history selection.
            conn.execute('UPDATE word_senses SET lexical_unit_id=? WHERE word_id=?', (members[0]['lexical_unit_id'], word['id']))
            members = conn.execute('SELECT * FROM word_senses WHERE word_id=? AND active=1 ORDER BY position', (word['id'],)).fetchall()
            unit = learning_units._merge_group(conn, word, {'key': 'test:merge', 'definition_cn': '测试', 'provenance': {'test': True}}, members, 'test:merge')
            chosen = conn.execute('SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL', (self.uid, unit)).fetchone()[0]
            self.assertEqual(chosen, first['example_id'])

    @unittest.skipUnless(BINARY.is_file(), 'build Rust CLI first')
    def test_native_repair_matches_python_and_dry_run_changes_nothing(self):
        self.rotating_card()
        native = self.directory/'native.db'
        with sqlite3.connect(database.DB_PATH) as source, sqlite3.connect(native) as target:
            source.backup(target)
        def cli(*args):
            result = subprocess.run([str(BINARY), 'fix-study-examples', '--database', str(native), *args], text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with sqlite3.connect(native) as conn:
            before = '\n'.join(conn.iterdump())
        self.assertTrue(cli('--dry-run')['dry_run'])
        with sqlite3.connect(native) as conn:
            self.assertEqual(before, '\n'.join(conn.iterdump()))
        with database.connect() as conn:
            expected = normalize_fixed_examples(conn)
        result = cli(); result.pop('dry_run')
        self.assertEqual(result, expected)
        self.assertEqual(cli()['cards_changed'], 0)
        for table in ('user_sense_examples', 'learning_units', 'review_history', 'sense_srs_state', 'study_attempts'):
            with sqlite3.connect(database.DB_PATH) as a, sqlite3.connect(native) as b:
                self.assertEqual(a.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall(), b.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall())
