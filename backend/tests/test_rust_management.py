"""Native administration, optimistic review exchange and subscription parity."""
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from app import database,push
from app.security import hash_password
import test_rust_api as reference
BINARY=reference.BINARY

@unittest.skipUnless(BINARY.is_file(),'build Rust server first')
class RustManagementTests(unittest.TestCase):
    stop=reference.RustApiParityTests.stop
    equivalent=reference.RustApiParityTests.equivalent
    pair=reference.RustApiParityTests.pair
    cards=reference.RustApiParityTests.cards
    def setUp(self):
        self.vapid_directory=tempfile.TemporaryDirectory();self.addCleanup(self.vapid_directory.cleanup)
        key=ec.generate_private_key(ec.SECP256R1());keypath=Path(self.vapid_directory.name)/'key.pem'
        keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.push_keys={'p256dh':base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)).decode().rstrip('='),'auth':base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip('=')}
        environment=patch.dict(os.environ,{'WEB_PUSH_PRIVATE_KEY':str(keypath),'WEB_PUSH_SUBJECT':'mailto:test@example.com','WEB_PUSH_ORIGINS':'http://localhost:5173'})
        environment.start();self.addCleanup(environment.stop);push.signing_key.cache_clear();self.addCleanup(push.signing_key.cache_clear)
        reference.RustApiParityTests.setUp(self)
    def admin(self):
        password=hash_password('admin-password-123')
        for path in [database.DB_PATH,self.native_db]:
            with sqlite3.connect(path) as c:c.execute("INSERT INTO users(username,password_hash,timezone,created_at,role) VALUES('admin',?,'Asia/Shanghai','2026-10-22T00:00:00+00:00','admin')",(password,))
        self.pair('POST','/api/auth/login',{'username':'admin','password':'admin-password-123'})
    def report(self):
        a,b=self.cards()
        self.pair('POST','/api/content-reports',{'attempt_id':a['attempt_id'],'category':'definition','details':'请核对释义'}, {'attempt_id':b['attempt_id'],'category':'definition','details':'请核对释义'})
    def test_authorization_and_management_queries(self):
        self.pair('GET','/api/admin/overview')
        self.report();self.admin()
        for path in ['/api/admin/overview','/api/admin/users','/api/admin/users/1','/api/admin/users/9999','/api/admin/reports','/api/admin/reports/1','/api/next','/api/settings','/api/push/config']:
            self.pair('GET',path)
        self.pair('PATCH','/api/admin/reports/1',{'status':'resolved','resolution_notes':'已经核对'})
        self.pair('PATCH','/api/admin/reports/1',{'status':'resolved'})
        self.pair('PATCH','/api/admin/reports/1',{'status':'pending'})
    def test_content_correction_snapshot_override_and_stale_rejection(self):
        self.report();self.admin();left,right=self.pair('GET','/api/admin/reports/1')
        payload={'expected_content':left['current_content'],'changes':{'definition_cn':'地址；住址','translation_cn':'请写下你的地址。'},'resolution_notes':'核对完成'}
        self.pair('PATCH','/api/admin/reports/1/content',payload)
        self.pair('PATCH','/api/admin/reports/1/content',payload)
        self.pair('GET','/api/admin/report-review/export')
        for table in ['admin_content_overrides','admin_content_edits','content_reports','meaning_catalog_revision']:
            values=[]
            for path in [database.DB_PATH,self.native_db]:
                with sqlite3.connect(path) as c:
                    c.row_factory=sqlite3.Row;records=[]
                    for row in c.execute('SELECT * FROM '+table+' ORDER BY rowid'):
                        record=dict(row)
                        for key in ['before_json','after_json','original_json','values_json','content_snapshot']:
                            if key in record:record[key]=json.loads(record[key])
                        records.append(record)
                    values.append(records)
            self.equivalent(*values)
    def test_review_batch_dry_run_atomic_apply_and_idempotency(self):
        self.report();self.admin();left,right=self.pair('GET','/api/admin/report-review/export')
        manifest=left['manifest'];result={'batch':manifest['batch'],'items':[{'id':1,'action':'correct','changes':[{'field':'translation_cn','value':'请写下你的地址。'}],'notes':'译文核对'}]}
        payload={'manifest':manifest,'result':result}
        self.pair('POST','/api/admin/report-review/validate',payload)
        self.pair('POST','/api/admin/report-review/import',payload)
        a=self.client.post('/api/admin/report-review/import?dry_run=false',json=payload)
        b=self.native.post('/api/admin/report-review/import?dry_run=false',json=payload)
        self.assertEqual(b.status_code,a.status_code,b.text);a,b=a.json(),b.json();self.assertTrue(Path(b['backup']).is_file());a['backup']=b['backup']=None;self.equivalent(a,b)
        self.pair('POST','/api/admin/report-review/import?dry_run=false',payload)
        self.pair('GET','/api/admin/reports/1')
        payload['manifest']['records'][0]['details']='篡改'
        self.pair('POST','/api/admin/report-review/validate',payload)
    def test_push_key_origin_conflict_and_account_ownership(self):
        self.pair('GET','/api/push/config')
        payload={'endpoint':'https://web.push.apple.com/test','keys':self.push_keys}
        self.client.headers['Origin']='http://localhost:5173'
        self.pair('POST','/api/push/subscription',payload)
        self.pair('POST','/api/push/subscription',payload)
        other={**payload,'keys':{**self.push_keys,'auth':base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip('=')}}
        self.pair('POST','/api/push/subscription',other)
        a=self.client.post('/api/push/subscription',json=payload,headers={'Origin':'https://bad.example'})
        b=self.native.post('/api/push/subscription',json=payload,headers={'Origin':'https://bad.example'})
        self.assertEqual(a.status_code,b.status_code);self.equivalent(a.json(),b.json())
        self.pair('POST','/api/auth/logout')
        for path in [database.DB_PATH,self.native_db]:
            with sqlite3.connect(path) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0],0)

    def test_correction_failure_rolls_back_content_overrides_and_receipts(self):
        self.report();self.admin();report=self.native.get('/api/admin/reports/1').json()
        with sqlite3.connect(self.native_db) as c:
            c.execute("CREATE TRIGGER fail_native_edit BEFORE INSERT ON admin_content_edits BEGIN SELECT RAISE(ABORT,'injected failure');END")
            before=list(c.iterdump())
        response=self.native.patch('/api/admin/reports/1/content',json={'expected_content':report['current_content'],'changes':{'definition_cn':'应该回滚的释义','translation_cn':'应该回滚的翻译'}})
        self.assertEqual(response.status_code,500)
        with sqlite3.connect(self.native_db) as c:self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(self.native.get('/api/admin/reports/1').json(),report)
