"""Python/Rust HTTP and database parity on independent, temporary v22 copies."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import httpx
from app import database
import test_senses as fixtures
from test_passkeys import SoftwareAuthenticator

ROOT=Path(__file__).resolve().parents[2]
BINARY=Path(os.environ.get('ENGLISH_TEST_RUST_SERVER',ROOT/'rust/target/debug/english-server'))
NOW=datetime(2026,10,22,4,tzinfo=timezone.utc)

@unittest.skipUnless(BINARY.is_file(),'build Rust server first')
class RustApiParityTests(unittest.TestCase):
    def setUp(self):
        fixtures.SenseLearningTests.setUp(self)
        self.native_db=self.directory/'native.db'
        with sqlite3.connect(database.DB_PATH) as source,sqlite3.connect(self.native_db) as target:source.backup(target)
        for module in ['app.security','app.main','app.sense_learning','app.adaptive_memory','app.learning_calendar','app.passkeys']:
            patcher=patch(module+'.datetime',wraps=datetime);clock=patcher.start();self.addCleanup(patcher.stop)
            clock.now.side_effect=lambda zone=None:NOW.astimezone(zone) if zone else NOW.replace(tzinfo=None)
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        self.log=(self.directory/'rust.log').open('wb');self.addCleanup(self.log.close)
        env={**os.environ,'WEBAUTHN_RP_ID':'localhost','WEBAUTHN_ORIGINS':'http://localhost:5173,http://localhost:8000','RUST_LOG':'warn'}
        self.process=subprocess.Popen([str(BINARY),'--database',str(self.native_db),'--listen',f'127.0.0.1:{port}','--enable-api','--test-clock',NOW.isoformat()],stdout=self.log,stderr=self.log,env=env)
        self.addCleanup(self.stop)
        self.native=httpx.Client(base_url=f'http://127.0.0.1:{port}',cookies=dict(self.client.cookies),headers={'Origin':'http://localhost:5173'},timeout=15)
        self.addCleanup(self.native.close)
        for _ in range(300):
            try:
                if self.native.get('/api/health').status_code==200:break
            except httpx.ConnectError:pass
            if self.process.poll() is not None: self.fail((self.directory/'rust.log').read_text())
            time.sleep(0.01)
        else:self.fail('Rust server did not become healthy')

    def stop(self):
        if self.process.poll() is None:self.process.terminate()
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)

    def equivalent(self,left,right):
        if isinstance(left,float) and isinstance(right,(float,int)):
            self.assertAlmostEqual(left,right,delta=1e-9*max(1,abs(left)));return
        if isinstance(left,dict):
            self.assertIsInstance(right,dict);self.assertEqual(set(left),set(right))
            for key in left:
                if key=='attempt_id' and left[key] is not None:continue
                self.equivalent(left[key],right[key])
        elif isinstance(left,list):
            self.assertIsInstance(right,list);self.assertEqual(len(left),len(right))
            for a,b in zip(left,right):self.equivalent(a,b)
        else:self.assertEqual(left,right)

    def pair(self,method,path,body=None,native_body=None):
        left=self.client.request(method,path,json=body)
        right=self.native.request(method,path,json=body if native_body is None else native_body)
        self.assertEqual(right.status_code,left.status_code,(path,right.text,(self.directory/'rust.log').read_text()))
        self.equivalent(left.json(),right.json())
        return left.json(),right.json()

    def cards(self):
        a,b=self.pair('GET','/api/next');return a['card'],b['card']

    def review(self,a,b,answer):
        payload=lambda card:{'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],
            'learning_unit_id':card['learning_unit_id'],'attempt_id':card['attempt_id'],'user_answer':answer,'active_response_ms':1234}
        return self.pair('POST','/api/review',payload(a),payload(b))

    def state(self):
        tables=['sense_srs_state','review_history','adaptive_memory','relearning_queue','user_sense_examples','sense_exposures','study_attempts']
        for table in tables:
            outputs=[]
            for path in [database.DB_PATH,self.native_db]:
                with sqlite3.connect(path) as conn:
                    conn.row_factory=sqlite3.Row;result=[]
                    for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                        record=dict(row)
                        if table=='study_attempts':record.pop('id',None)
                        if 'attempt_id' in record and record['attempt_id'] is not None:record['attempt_id']='<random>'
                        if 'card_json' in record:
                            card=json.loads(record['card_json'])
                            for key in ['due','last_review']:
                                if card[key]:card[key]=datetime.fromisoformat(card[key]).isoformat()
                            record['card_json']=card
                        result.append(record)
                    outputs.append(result)
            self.equivalent(*outputs)

    def test_old_session_password_settings_dictionary_and_word_lists(self):
        for path in ['/api/auth/me','/api/settings','/api/word-lists','/api/dictionary/address','/api/dictionary/unknown','/api/muted-words']:
            self.pair('GET',path)
        self.pair('PATCH','/api/settings',{'show_sentence_translation':True,'speech_rate':120,'selected_word_list_ids':['cet4']})
        self.pair('POST','/api/auth/logout')
        self.pair('POST','/api/auth/login',{'username':'learner','password':'test-password-123'})
        self.pair('PATCH','/api/auth/me',{'timezone':'America/New_York'})
        self.pair('PATCH','/api/auth/password',{'current_password':'test-password-123','new_password':'next-password-123'})
        self.pair('POST','/api/auth/logout')
        self.pair('POST','/api/auth/login',{'username':'learner','password':'next-password-123'})

    def test_native_catalogue_exact_body_304_and_revision_invalidation(self):
        first=self.client.get('/api/study/meaning-catalog');native=self.native.get('/api/study/meaning-catalog')
        self.assertEqual(native.status_code,200,native.text)
        self.assertEqual(native.content,first.content);self.assertEqual(native.headers['etag'],first.headers['etag'])
        cached=self.native.get('/api/study/meaning-catalog',headers={'If-None-Match':native.headers['etag']})
        self.assertEqual(cached.status_code,304);self.assertEqual(cached.content,b'')
        for path in [database.DB_PATH,self.native_db]:
            with sqlite3.connect(path) as conn:conn.execute("UPDATE sense_examples SET translation_cn='新译文' WHERE id=(SELECT MIN(id) FROM sense_examples)")
        self.pair('GET','/api/study/meaning-catalog')

    def test_request_validation_and_coercion_match_frozen_contract(self):
        for method,path,body in [
            ('POST','/api/auth/register',{}),
            ('POST','/api/auth/login',{'username':'','password':''}),
            ('POST','/api/review',{'word_id':'invalid'}),
            ('PATCH','/api/settings',{'speech_rate':'120'}),
            ('PATCH','/api/settings',{'show_sentence_translation':'yes'}),
            ('POST','/api/auth/passkeys/login/verify',{}),
            ('POST','/api/auth/passkeys/register/options',{'current_password':'test-password-123','name':''}),
        ]:
            self.pair(method,path,body)

    def test_invalid_origin_signature_and_counter_cannot_replay_challenge(self):
        software=SoftwareAuthenticator()
        request=lambda path,body:self.native.post('/api/auth/passkeys/'+path,json=body)
        denied=self.native.post('/api/auth/passkeys/login/options',json={},headers={'Origin':'https://evil.example'})
        self.assertEqual(denied.status_code,403)
        options=request('register/options',{'current_password':'test-password-123'});self.assertEqual(options.status_code,200,options.text)
        self.assertEqual(request('register/verify',{'credential':software.registration(options.json()['options'])}).status_code,200)
        self.native.post('/api/auth/logout');options=request('login/options',{}).json()['options']
        credential=software.authentication(options);credential['response']['signature']='AAAA'
        self.assertEqual(request('login/verify',{'credential':credential}).status_code,400)
        self.assertEqual(request('login/verify',{'credential':software.authentication(options)}).status_code,400)
        options=request('login/options',{}).json()['options'];self.assertEqual(request('login/verify',{'credential':software.authentication(options,count=2)}).status_code,200)
        options=request('login/options',{}).json()['options'];self.assertEqual(request('login/verify',{'credential':software.authentication(options,count=1)}).status_code,400)

    def test_learning_wrong_correction_independent_statistics_and_calendar(self):
        self.pair('GET','/api/stats')
        a,b=self.cards();self.review(a,b,'wrong');self.state()
        self.review(a,b,a['answer_form']);self.state()
        a,b=self.cards();self.review(a,b,a['answer_form']);self.state()
        self.pair('GET','/api/stats');self.pair('GET','/api/learning-calendar?month=2026-10')

    def test_delayed_fsrs_multiplier_and_maturity_boundaries_match(self):
        from datetime import timedelta
        from fsrs import Card,State
        for stability,multiplier in [(1,0.5),(20,1),(100,2),(365,0.5)]:
            for path in [database.DB_PATH,self.native_db]:
                with sqlite3.connect(path) as conn:
                    for table in ['review_history','sense_exposures','study_attempts','user_sense_examples','relearning_queue','adaptive_memory','sense_srs_state','memory_profiles']:
                        conn.execute(f'DELETE FROM {table} WHERE user_id=?',(self.uid,))
            a,b=self.cards()
            card=Card(card_id=a['learning_unit_id'],state=State.Review,step=None,stability=stability,difficulty=5,
                due=NOW,last_review=NOW-timedelta(days=30))
            for path in [database.DB_PATH,self.native_db]:
                with sqlite3.connect(path) as conn:
                    anchor=conn.execute('SELECT representative_sense_id FROM learning_units WHERE id=?',(a['learning_unit_id'],)).fetchone()[0]
                    conn.execute('INSERT INTO sense_srs_state(user_id,sense_id,learning_unit_id,review_count,correct_count,wrong_count,lapse_count,easiness_factor,interval_days,next_review_date,status,last_example_id) VALUES(?,?,?,1,1,0,0,2.5,30,?,?,?)',
                        (self.uid,anchor,a['learning_unit_id'],NOW.date().isoformat(),'Reviewing',a['example_id']))
                    conn.execute('INSERT INTO adaptive_memory(user_id,sense_id,learning_unit_id,card_json) VALUES(?,?,?,?)',(self.uid,anchor,a['learning_unit_id'],card.to_json()))
                    conn.execute('INSERT INTO memory_profiles(user_id,forgetting_multiplier) VALUES(?,?)',(self.uid,multiplier))
            self.review(a,b,a['answer_form']);self.state();self.pair('GET','/api/stats')

    def test_hints_meanings_exposures_and_mute_preserve_state(self):
        a,b=self.cards();self.pair('POST','/api/study/hint',{'attempt_id':a['attempt_id'],'kind':'pronunciation'},
            {'attempt_id':b['attempt_id'],'kind':'pronunciation'})
        self.review(a,b,a['answer_form']);self.state()
        a,b=self.cards();self.pair('POST','/api/study/meanings',{'attempt_id':a['attempt_id']},{'attempt_id':b['attempt_id']})
        self.state();self.pair('POST',f'/api/words/{a["id"]}/mute',{})
        self.pair('GET','/api/muted-words');self.pair('DELETE',f'/api/muted-words/{a["word"]}')

    def test_new_registration_and_duplicate_review(self):
        self.pair('POST','/api/auth/register',{'username':'second','password':'another-password','timezone':'Asia/Shanghai'})
        a,b=self.cards();self.review(a,b,a['answer_form']);self.review(a,b,a['answer_form']);self.state()

    def test_passkey_registration_and_real_signature_authentication(self):
        software=SoftwareAuthenticator()
        options=self.native.post('/api/auth/passkeys/register/options',json={'name':'测试密钥','current_password':'test-password-123'})
        self.assertEqual(options.status_code,200,options.text)
        registered=self.native.post('/api/auth/passkeys/register/verify',json={'credential':software.registration(options.json()['options'])})
        self.assertEqual(registered.status_code,200,registered.text)
        self.native.post('/api/auth/logout')
        options=self.native.post('/api/auth/passkeys/login/options',json={})
        self.assertEqual(options.status_code,200,options.text)
        login=self.native.post('/api/auth/passkeys/login/verify',json={'credential':software.authentication(options.json()['options'])})
        self.assertEqual(login.status_code,200,login.text)
        replay=self.native.post('/api/auth/passkeys/login/verify',json={'credential':software.authentication(options.json()['options'],count=2)})
        self.assertEqual(replay.status_code,400)

    def test_old_python_passkey_is_accepted_by_native_verifier(self):
        from app import passkeys
        with patch.object(passkeys,'RP_ID','localhost'),patch.object(passkeys,'ORIGINS',['http://localhost:5173']):
            self.client.headers['Origin']='http://localhost:5173';software=SoftwareAuthenticator()
            options=self.client.post('/api/auth/passkeys/register/options',json={'name':'旧密钥','current_password':'test-password-123'}).json()['options']
            from app.security import hash_token,utc_now_iso
            token=self.client.cookies.get('cvt_passkey_registration')
            self.assertIsNotNone(token,'reference challenge cookie missing')
            with database.connect() as conn:
                stored=conn.execute('SELECT * FROM webauthn_challenges WHERE token_hash=?',(hash_token(token),)).fetchone()
                self.assertIsNotNone(stored,'reference challenge missing')
                self.assertEqual(stored['purpose'],'registration')
                self.assertGreater(stored['expires_at'],utc_now_iso())
                self.assertEqual(stored['user_id'],self.uid)
                self.assertEqual(stored['session_hash'],hash_token(self.client.cookies.get('cvt_session')))
            response=self.client.post('/api/auth/passkeys/register/verify',json={'credential':software.registration(options)})
            self.assertEqual(response.status_code,200,response.text)
        with sqlite3.connect(database.DB_PATH) as source,sqlite3.connect(self.native_db) as target:
            for table in ['webauthn_users','passkeys']:
                for row in source.execute(f'SELECT * FROM {table}'):
                    target.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in row)})',row)
        self.native.post('/api/auth/logout');options=self.native.post('/api/auth/passkeys/login/options',json={})
        self.assertEqual(options.status_code,200,options.text)
        result=self.native.post('/api/auth/passkeys/login/verify',json={'credential':software.authentication(options.json()['options'])})
        self.assertEqual(result.status_code,200,result.text)
