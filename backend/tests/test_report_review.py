from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
from app import database
from app import report_review
from app.main import app
import test_admin as admin_fixtures
import test_senses as fixtures

sys.path.insert(0,str(database.BASE_DIR/'scripts'))
from report_exchange import export_batches
from translation_exchange import DEFAULT_CATALOG, run_batches


class ReportReviewTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    admin_client = admin_fixtures.AdminTests.admin_client

    def create_report(self, card, category='definition'):
        response = self.client.post('/api/content-reports',json={'attempt_id':card['attempt_id'],
                                    'category':category,'details':'请核对当前语境'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['id']

    def export(self, admin):
        response = admin.get('/api/admin/report-review/export')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['manifest']

    def result(self, manifest, action='correct', changes=None, notes='已按语境核对'):
        return {'batch':manifest['batch'],'items':[{'id':r['id'],'action':action,
                 'changes':[{'field':key,'value':value} for key,value in (changes or {}).items()],
                 'notes':notes} for r in manifest['records']]}

    def test_permissions_and_export_exclude_account_information(self):
        card=self.next();self.create_report(card)
        path='/api/admin/report-review/export'
        self.assertEqual(TestClient(app).get(path).status_code,401)
        self.assertEqual(self.client.get(path).status_code,403)
        admin=self.admin_client();manifest=self.export(admin)
        self.assertEqual(len(manifest['records']),1)
        content=manifest['records'][0]['expected_content']
        self.assertEqual(content['word'],'address')
        for key in ('username','user_id','attempt_id','password_hash'):
            self.assertNotIn(key,json.dumps(manifest))
        self.assertEqual(admin.get(path+'?limit=1001').status_code,422)
        payload={'manifest':manifest,'result':self.result(manifest,'manual')}
        for endpoint in ('validate','import'):
            self.assertEqual(self.client.post('/api/admin/report-review/'+endpoint,json=payload).status_code,403)

    def test_preview_import_idempotency_and_catalog_refresh_preserve_progress(self):
        card=self.next();self.review(card,'bad');rid=self.create_report(card)
        admin=self.admin_client();manifest=self.export(admin)
        payload={'manifest':manifest,'result':self.result(manifest,changes={'definition_cn':'居住地址','translation_cn':'我们的警员去了那个住址。'})}
        with database.connect() as conn:
            before={table:[tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('review_history','sense_srs_state','adaptive_memory')}
        preview=admin.post('/api/admin/report-review/validate',json=payload)
        self.assertEqual(preview.status_code,200,preview.text)
        dry=admin.post('/api/admin/report-review/import',json=payload)
        self.assertEqual(dry.status_code,200,dry.text);self.assertTrue(dry.json()['dry_run'])
        self.assertIsNone(dry.json()['backup'])
        self.assertEqual(admin.get(f'/api/admin/reports/{rid}').json()['status'],'pending')
        applied=admin.post('/api/admin/report-review/import?dry_run=false',json=payload)
        self.assertEqual(applied.status_code,200,applied.text)
        self.assertEqual(applied.json()['corrected'],1)
        self.assertTrue(Path(applied.json()['backup']).exists())
        retry=admin.post('/api/admin/report-review/import?dry_run=false',json=payload)
        self.assertEqual(retry.status_code,200,retry.text)
        self.assertEqual(retry.json()['already_applied'],1)
        self.assertIsNone(retry.json()['backup'])
        raw=json.loads(self.catalog_path.read_text());raw['lists'][0]['description']='refresh'
        self.catalog_path.write_text(json.dumps(raw));database.init_database()
        fixed=admin.get(f'/api/admin/reports/{rid}').json()
        self.assertEqual(fixed['current_content']['definition_cn'],'居住地址')
        self.assertEqual(fixed['current_content']['translation_cn'],'我们的警员去了那个住址。')
        self.assertEqual(len(fixed['edits']),1)
        with database.connect() as conn:
            for table,rows in before.items():
                self.assertEqual([tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')],rows)

    def test_mixed_decisions_keep_uncertain_reports_pending(self):
        card=self.next();self.create_report(card);manual_id=self.create_report(card,'pronunciation')
        admin=self.admin_client();manifest=self.export(admin)
        result=self.result(manifest,'no_change')
        result['items'][1]['action']='manual'
        response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':result})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((response.json()['confirmed'],response.json()['manual']),(1,1))
        self.assertEqual(admin.get(f'/api/admin/reports/{manual_id}').json()['status'],'pending')

    def test_bad_or_stale_results_make_no_partial_changes(self):
        card=self.next();self.create_report(card);self.create_report(card,'translation')
        admin=self.admin_client();manifest=self.export(admin)
        good=self.result(manifest,changes={'definition_cn':'住址'})
        cases=[]
        bad=json.loads(json.dumps(good));bad['batch']='wrong';cases.append(bad)
        bad=json.loads(json.dumps(good));bad['items'][1]['id']=bad['items'][0]['id'];cases.append(bad)
        bad=json.loads(json.dumps(good));bad['items'].pop();cases.append(bad)
        bad=json.loads(json.dumps(good));bad['items'][1]['changes'][0]['value']='另一个互相矛盾的释义';cases.append(bad)
        bad=self.result(manifest,changes={'sentence':'Missing target entirely.'});cases.append(bad)
        bad=self.result(manifest,changes={'status':'resolved'});cases.append(bad)
        for result in cases:
            response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':result})
            self.assertIn(response.status_code,(400,409,422),response.text)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_content_edits').fetchone()[0],0)
            conn.execute("UPDATE word_senses SET definition_cn='已经更新' WHERE id=?",(card['sense_id'],))
        response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':good})
        self.assertEqual(response.status_code,422,response.text)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_reports WHERE status='resolved'").fetchone()[0],0)

    def test_shared_content_corrections_merge_without_duplicate_edits(self):
        card=self.next();self.create_report(card);self.create_report(card,'translation')
        admin=self.admin_client();manifest=self.export(admin)
        result=self.result(manifest,changes={'definition_cn':'住址'})
        response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':result})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['corrected'],2)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_content_edits').fetchone()[0],1)
        retry=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':result})
        self.assertEqual(retry.status_code,200,retry.text)
        self.assertEqual(retry.json()['already_applied'],2)

    def test_cli_export_and_model_runner_only_create_proposals(self):
        card=self.next();self.create_report(card)
        folder=self.directory/'report-batches'
        self.assertEqual(export_batches(database.DB_PATH,folder)['exported_reports'],1)
        manifest=json.loads((folder/'batch-0001.manifest.json').read_text())
        result=self.result(manifest,'manual')
        def fake_run(command,**kwargs):
            self.assertIn('gpt-6-luna',command)
            self.assertIn('model_reasoning_effort="low"',command)
            self.assertIn('--ephemeral',command)
            self.assertNotIn('username',kwargs['input'])
            Path(command[command.index('--output-last-message')+1]).write_text(json.dumps(result))
        with patch('translation_exchange.subprocess.run',side_effect=fake_run):
            self.assertEqual(run_batches(folder,DEFAULT_CATALOG)['completed_batches'],1)
            self.assertEqual(run_batches(folder,DEFAULT_CATALOG)['skipped_batches'],1)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_reports WHERE status='pending'").fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_content_edits').fetchone()[0],0)

    def test_mid_import_failure_rolls_back_the_entire_batch(self):
        card=self.next();self.create_report(card);self.create_report(card,'translation')
        admin=self.admin_client();manifest=self.export(admin)
        result=self.result(manifest,changes={'definition_cn':'住址'})
        result['items'][1]['changes']=[{'field':'translation_cn','value':'我们的警员去了那个住址。'}]
        real=report_review.correct_content_from
        count=0
        def failing_second(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:
                raise HTTPException(409,'Conflict during import')
            return real(*args,**kwargs)
        with patch.object(report_review,'correct_content_from',side_effect=failing_second):
            response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,'result':result})
        self.assertEqual(response.status_code,409,response.text)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT definition_cn FROM word_senses WHERE id=?',(card['sense_id'],)).fetchone()[0],card['definition_cn'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_content_edits').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_content_overrides').fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_reports WHERE status='resolved'").fetchone()[0],0)

    def test_archived_report_cannot_be_corrected_by_an_old_batch(self):
        card=self.next();self.create_report(card)
        admin=self.admin_client();manifest=self.export(admin)
        with database.connect() as conn:
            conn.execute('UPDATE word_senses SET active=0 WHERE id=?',(card['sense_id'],))
        response=admin.post('/api/admin/report-review/import?dry_run=false',json={'manifest':manifest,
                           'result':self.result(manifest,changes={'definition_cn':'住址'})})
        self.assertEqual(response.status_code,422,response.text)
