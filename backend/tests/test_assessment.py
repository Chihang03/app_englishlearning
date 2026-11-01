"""Native CAT integration against isolated databases; no live data or fake calibration."""
import hashlib,json,os,sqlite3,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
CTL=Path(os.environ.get('ENGLISH_TEST_NATIVE_CATALOG',ROOT/'rust/target/debug/englishctl'))
SERVER=Path(os.environ.get('ENGLISH_TEST_RUST_SERVER',ROOT/'rust/target/debug/english-server'))
import socket,time,httpx
from contextlib import contextmanager
@contextmanager
def connect(path):
    c=sqlite3.connect(path)
    try:
        with c:yield c
    finally:c.close()


class AssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.directory=Path(cls.temp.name);cls.db=cls.directory/'test.db'
        subprocess.run([str(CTL),'init','--database',str(cls.db)],check=True,capture_output=True)
        subprocess.run([str(CTL),'assessment-install','--database',str(cls.db),'--backup',str(cls.directory/'before.db')],check=True,capture_output=True)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        cls.log=open(cls.directory/'server.log','w')
        cls.proc=subprocess.Popen([str(SERVER),'--database',str(cls.db),'--listen',f'127.0.0.1:{port}','--enable-api','--test-clock','2026-10-30T10:00:00+00:00'],stdout=cls.log,stderr=cls.log)
        cls.client=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=30)
        for _ in range(100):
            try:
                if cls.client.get('/api/health').status_code==200:break
            except httpx.ConnectError:pass
            time.sleep(.05)
        else:raise RuntimeError((cls.directory/'server.log').read_text())
    @classmethod
    def tearDownClass(cls):
        cls.client.close();cls.proc.terminate();cls.proc.wait(timeout=10);cls.log.close();cls.temp.cleanup()
    def setUp(self):
        # Independent account/session without testing password hash on each case.
        self.token=os.urandom(24).hex()
        with connect(self.db) as c:
            self.uid=c.execute("INSERT INTO users(username,password_hash,timezone,created_at) VALUES(?,'unused','Asia/Shanghai','2026-10-01')",('assessment-'+self.token,)).lastrowid
            c.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,'2026-10-01','2027-01-01')",(hashlib.sha256(self.token.encode()).hexdigest(),self.uid))
        self.client.cookies.set('cvt_session',self.token)
        # Actual cookie name checked below by the server config fixture.
    def call(self,method,path,body=None,status=200):
        response=self.client.request(method,path,json=body)
        self.assertEqual(response.status_code,status,response.text+((self.directory/"server.log").read_text()[-2500:] if response.status_code==500 else ""))
        return response.json()
    def start(self):return self.call('POST','/api/assessments',{})
    def answer(self,sess,correct=True):
        q=sess['question'];self.assertNotIn('answer',q);self.assertNotIn('item_id',q)
        with connect(self.db) as c:
            payload,order=c.execute('SELECT i.payload,r.option_order FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND sequence=?',(sess['id'],q['sequence'])).fetchone()
        item=json.loads(payload)
        answer=str(json.loads(order).index(int(item['answer']))) if q['kind']=='recognition' else item['answer']
        body={'sequence':q['sequence'],'answer':answer if correct else ''}
        next_=self.call('POST',f"/api/assessments/{sess['id']}/answers",body)
        again=self.call('POST',f"/api/assessments/{sess['id']}/answers",body)
        self.assertEqual(next_,again)
        return next_
    def complete(self,correct=True):
        sess=self.start()
        for _ in range(40):
            if sess['state']!='active':break
            sess=self.answer(sess,correct)
        self.assertEqual(sess['state'],'completed')
        return sess
    def test_full_test_apply_and_no_srs_mutation(self):
        sess=self.complete()
        self.assertTrue(sess['result']['sufficient']);self.assertEqual(sess['result']['recall']['answered'],4)
        self.assertGreaterEqual(sess['answered'],28);self.assertLessEqual(sess['answered'],40)
        with connect(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(DISTINCT i.family),COUNT(*) FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE session_id=?',(sess['id'],)).fetchone(),(sess['answered'],sess['answered']))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM review_history WHERE user_id=?',(self.uid,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sense_srs_state WHERE user_id=?',(self.uid,)).fetchone()[0],0)
            c.execute("INSERT INTO settings VALUES(?,'skip_basic_600','true')",(self.uid,))
        applied=self.call('POST',f"/api/assessments/{sess['id']}/apply",{})
        self.assertTrue(applied['applied'])
        self.assertEqual(self.call('GET','/api/proficiency')['mode'],'adaptive')
        self.call('PATCH','/api/proficiency/mode',{'mode':'frequency'})
        with connect(self.db) as c:self.assertEqual(c.execute("SELECT value FROM settings WHERE user_id=? AND key='skip_basic_600'",(self.uid,)).fetchone()[0],'true')
    def test_resume_conflict_ownership_and_answers_hidden(self):
        sess=self.start();self.assertEqual(self.start(),sess)
        self.assertEqual(self.call('GET',f"/api/assessments/{sess['id']}"),sess)
        next_=self.answer(sess)
        self.call('POST',f"/api/assessments/{sess['id']}/answers",{'sequence':1,'answer':''},status=409)
        self.call('POST',f"/api/assessments/{sess['id']}/answers",{'sequence':999,'answer':''},status=409)
        with connect(self.db) as c:
            other=c.execute("INSERT INTO users(username,password_hash,timezone,created_at) VALUES(?,'unused','UTC','2026-10-01')",('other-'+self.token,)).lastrowid
            c.execute('UPDATE assessment_sessions SET user_id=? WHERE id=?',(other,sess['id']))
        self.call('GET',f"/api/assessments/{sess['id']}",status=404)
    def test_all_wrong_and_abandon(self):
        sess=self.complete(False);self.assertTrue(sess['result']['boundary'])
        self.assertLess(sess['result']['estimate']['mean'],-2)
        new=self.start();self.call('POST',f"/api/assessments/{new['id']}/apply",{},status=409)
        abandoned=self.call('POST',f"/api/assessments/{new['id']}/abandon",{})
        self.assertIsNone(abandoned['question'])
    def test_mutes_and_pool_exhaustion(self):
        with connect(self.db) as c:
            c.execute("INSERT INTO user_muted_words SELECT ?,word,'2026-10-01' FROM assessment_items GROUP BY word",(self.uid,))
        sess=self.start();self.assertEqual(sess['state'],'completed');self.assertFalse(sess['result']['sufficient'])
        self.call('POST',f"/api/assessments/{sess['id']}/apply",{},status=409)

    def test_adaptive_ranking_uses_unit_difficulty_and_respects_mutes(self):
        sess=self.start()
        with connect(self.db) as c:
            c.execute("INSERT INTO user_proficiency_profiles(user_id,session_id,mode,mean,sd,initial_mean,model_version,updated_at) VALUES(?,?,'adaptive',0,.5,0,'vocab-grid-v1','2026-10-30')",(self.uid,sess['id']))
            for word,difficulty in [('easytest',-2),('middletest',0),('hardtest',2)]:
                wid=c.execute("INSERT INTO words(owner_id,word,part_of_speech,definition_cn,example_sentence) VALUES(?,?,'名词','测试',?)",(self.uid,word,'This is '+word+'.')).lastrowid
                lid=c.execute("INSERT INTO lexical_units(owner_id,word_id,unit_key,headword,normalized_headword,pos_group,identity_status) VALUES(?,?,?,?,?,'noun','verified')",(self.uid,wid,word,word,word)).lastrowid
                sid=c.execute("INSERT INTO word_senses(word_id,sense_key,part_of_speech,definition_cn,source,lexical_unit_id) VALUES(?,'test','名词','测试','test',?)",(wid,lid)).lastrowid
                c.execute("INSERT INTO sense_examples(sense_id,sentence,target_form,source) VALUES(?,?,?,'test')",(sid,'This is '+word+'.',word))
                unit=c.execute("INSERT INTO learning_units(lexical_unit_id,word_id,unit_key,representative_sense_id,display_definition_cn,verification) VALUES(?,?,'test',?,'测试','reviewed')",(lid,wid,sid)).lastrowid
                c.execute("INSERT INTO learning_unit_senses(sense_id,learning_unit_id) VALUES(?,?)",(sid,unit))
                c.execute("INSERT INTO learning_unit_difficulty(learning_unit_id,difficulty,source,verified) VALUES(?,?,'test',0)",(unit,difficulty))
        card=self.call('GET','/api/next')['card'];self.assertEqual(card['word'],'middletest')
        self.call('POST',f"/api/words/{card['id']}/mute",{})
        self.assertNotEqual(self.call('GET','/api/next')['card']['word'],'middletest')
        self.assertTrue(self.call('GET','/api/proficiency')['automatic_deferral'])
        next_card=self.call('GET','/api/next')['card']
        def review(card):
            return self.call('POST','/api/review',{'attempt_id':card['attempt_id'],'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],'learning_unit_id':card['learning_unit_id'],'user_answer':card['answer_form'],'active_response_ms':1200})
        self.assertTrue(review(next_card)['is_correct'])
        with connect(self.db) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM proficiency_events WHERE user_id=? AND kind='first_answer'",(self.uid,)).fetchone()[0],1)
        hinted=self.call('GET','/api/next')['card']
        self.call('POST','/api/study/hint',{'attempt_id':hinted['attempt_id'],'kind':'pronunciation'})
        review(hinted)
        with connect(self.db) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM proficiency_events WHERE user_id=? AND kind='first_answer'",(self.uid,)).fetchone()[0],1)


    def test_install_is_additive_and_backup_refuses_overwrite(self):
        path=self.directory/(self.token+'.backup')
        proc=subprocess.run([str(CTL),'assessment-install','--database',str(self.db),'--backup',str(path)],capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        with connect(path) as c:self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],22)
        repeat=subprocess.run([str(CTL),'assessment-install','--database',str(self.db),'--backup',str(path)],capture_output=True)
        self.assertNotEqual(repeat.returncode,0)

    def profile_fixture(self):
        with connect(self.db) as c:
            sid='fixture-'+self.token
            c.execute("INSERT INTO assessment_sessions(id,user_id,state,bank_version,model_version,created_at,result_json,applied_at) VALUES(?,?,'completed','original-2026-10-v1','vocab-grid-v1','2026-10-01','{}','2026-10-01')",(sid,self.uid))
            c.execute("INSERT INTO user_proficiency_profiles(user_id,session_id,mode,mean,sd,initial_mean,model_version,updated_at) VALUES(?,?,'frequency',0,.5,0,'vocab-grid-v1','2026-10-01')",(self.uid,sid))
        self.call('PATCH','/api/proficiency/mode',{'mode':'adaptive'})

    def seed_unit(self,word='airport',meaning='机场'):
        with connect(self.db) as c:
            wid=c.execute("INSERT INTO words(owner_id,word,part_of_speech,definition_cn,example_sentence) VALUES(?,?,'名词',?,?)",(self.uid,word,meaning,'This is '+word+'.')).lastrowid
            lid=c.execute("INSERT INTO lexical_units(owner_id,word_id,unit_key,headword,normalized_headword,pos_group,identity_status) VALUES(?,?,?,?,?,'noun','verified')",(self.uid,wid,str(wid),word,word)).lastrowid
            sense=c.execute("INSERT INTO word_senses(word_id,sense_key,part_of_speech,definition_cn,source,lexical_unit_id) VALUES(?,'test','名词',?,'test',?)",(wid,meaning,lid)).lastrowid
            ex=c.execute("INSERT INTO sense_examples(sense_id,sentence,target_form,source) VALUES(?,?,?,'test')",(sense,'This is '+word+'.',word)).lastrowid
            unit=c.execute("INSERT INTO learning_units(lexical_unit_id,word_id,unit_key,representative_sense_id,display_definition_cn,verification) VALUES(?,?,'test',?,?,'reviewed')",(lid,wid,sense,meaning)).lastrowid
            c.execute('INSERT INTO learning_unit_senses VALUES(?,?)',(sense,unit))
        return wid,lid,sense,ex,unit

    def defer_fixture(self,unit,lid,meaning='机场'):
        with connect(self.db) as c:
            c.execute("INSERT INTO user_unit_readiness VALUES(?,?,'deferred','{}',1,'2026-10-29T10:00:00+00:00')",(self.uid,unit))
            c.execute("INSERT INTO readiness_details VALUES(?,?,'personal_confirmation','fixture','2026-11-30T10:00:00+00:00',?,?)",(self.uid,unit,meaning,lid))

    def test_mode_preserves_baseline_and_no_fallback(self):
        self.profile_fixture();self.seed_unit('about','关于')
        self.call('PATCH','/api/settings',{'skip_basic_600':True})
        before=self.call('GET','/api/stats')
        for mode in ('frequency','adaptive','frequency','adaptive'):
            self.call('PATCH','/api/proficiency/mode',{'mode':mode})
            self.assertTrue(self.call('GET','/api/settings')['skip_basic_600'])
            self.assertEqual(self.call('GET','/api/stats')['new_senses'],before['new_senses'])
            self.assertIsNone(self.call('GET','/api/next')['card'])

    def test_deferred_due_queue_stats_restore_and_account_isolation(self):
        self.profile_fixture();wid,lid,sid,ex,unit=self.seed_unit()
        card=self.call('GET','/api/next')['card']
        self.call('POST','/api/review',{'attempt_id':card['attempt_id'],'word_id':wid,'sense_id':sid,'example_id':ex,'learning_unit_id':unit,'user_answer':'airport'})
        with connect(self.db) as c:c.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=?",(self.uid,))
        self.assertEqual(self.call('GET','/api/stats')['due_words'],1)
        self.defer_fixture(unit,lid)
        self.assertEqual(self.call('GET','/api/stats')['due_words'],0)
        self.assertEqual(self.call('GET','/api/proficiency/readiness')['summary']['deferred_units'],1)
        self.assertIsNone(self.call('GET','/api/next')['card'])
        self.call('POST','/api/review',{'attempt_id':card['attempt_id'],'word_id':wid,'sense_id':sid,'example_id':ex,'learning_unit_id':unit,'user_answer':'airport'},status=409)
        self.call('POST','/api/proficiency/readiness',{'learning_unit_id':unit+999},status=404)
        self.call('POST','/api/proficiency/readiness',{'learning_unit_id':unit})
        self.assertEqual(self.call('GET','/api/stats')['due_words'],1)
        with connect(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM review_history WHERE user_id=?',(self.uid,)).fetchone()[0],1)

    def test_scope_confirmation_really_excludes_without_calibration(self):
        self.profile_fixture();self.seed_unit()
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)
        sess=self.call('POST','/api/proficiency/confirmations',{'band':1})
        for _ in range(12):
            if sess['state']!='active':break
            sess=self.answer(sess)
        self.assertEqual(sess['answered'],12);self.assertTrue(sess['result']['passed'])
        self.assertGreaterEqual(sess['preview']['eligible_scope_units'],1)
        self.call('POST',f"/api/assessments/{sess['id']}/apply",{})
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],0)
        self.assertIsNone(self.call('GET','/api/next')['card'])
        self.call('PATCH','/api/proficiency/mode',{'mode':'frequency'})
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)

    def test_failed_confirmation_cannot_defer_and_expiry_restores(self):
        self.profile_fixture();_,lid,_,_,unit=self.seed_unit()
        sess=self.call('POST','/api/proficiency/confirmations',{'band':1})
        for _ in range(12):
            if sess['state']!='active':break
            sess=self.answer(sess,False)
        self.assertFalse(sess['result']['passed'])
        self.call('POST',f"/api/assessments/{sess['id']}/apply",{},status=409)
        self.defer_fixture(unit,lid)
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],0)
        with connect(self.db) as c:c.execute("UPDATE readiness_details SET valid_until='2026-10-29' WHERE user_id=?",(self.uid,))
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)

    def test_personal_proof_across_days_and_contexts(self):
        self.profile_fixture();wid,lid,sense,ex,unit=self.seed_unit()
        with connect(self.db) as c:
            ex2=c.execute("INSERT INTO sense_examples(sense_id,sentence,target_form,source) VALUES(?,'We left the airport.','airport','test')",(sense,)).lastrowid
            for day,example in [('2026-10-25',ex),('2026-10-27',ex2)]:
                c.execute("INSERT INTO review_history(user_id,word_id,review_time,user_answer,is_correct,sense_id,example_id,is_independent,is_first_attempt,learning_unit_id) VALUES(?,?,?,'airport',1,?,?,1,1,?)",(self.uid,wid,day+'T09:00:00+00:00',sense,example,unit))
        self.call('PATCH','/api/proficiency/mode',{'mode':'adaptive'})
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],0)
        self.assertEqual(self.call('GET','/api/proficiency/readiness')['summary']['deferred_units'],1)
        with connect(self.db) as c:
            c.execute("INSERT INTO review_history(user_id,word_id,review_time,user_answer,is_correct,sense_id,example_id,is_independent,is_first_attempt,learning_unit_id) VALUES(?,?,?,'wrong',0,?,?,0,1,?)",(self.uid,wid,'2026-10-30T09:00:00+00:00',sense,ex,unit))
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)

    def test_partial_word_deferral_does_not_inflate_mastery(self):
        self.profile_fixture();wid,lid,sid,ex,unit=self.seed_unit()
        card=self.call('GET','/api/next')['card']
        self.call('POST','/api/review',{'attempt_id':card['attempt_id'],'word_id':wid,'sense_id':sid,'example_id':ex,'learning_unit_id':unit,'user_answer':'airport'})
        with connect(self.db) as c:
            c.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=?",(self.uid,))
            second=c.execute("INSERT INTO word_senses(word_id,sense_key,part_of_speech,definition_cn,source,lexical_unit_id) VALUES(?,'special','名词','特殊义项','test',?)",(wid,lid)).lastrowid
            c.execute("INSERT INTO sense_examples(sense_id,sentence,target_form,source) VALUES(?,'An airport opened.','airport','test')",(second,))
            unit2=c.execute("INSERT INTO learning_units(lexical_unit_id,word_id,unit_key,representative_sense_id,display_definition_cn,verification) VALUES(?,?,'special',?,'特殊义项','reviewed')",(lid,wid,second)).lastrowid
            c.execute('INSERT INTO learning_unit_senses VALUES(?,?)',(second,unit2))
        before=self.call('GET','/api/stats')['mastered']
        self.defer_fixture(unit2,lid,'特殊义项')
        self.assertEqual(self.call('GET','/api/stats')['mastered'],before)

    def test_readiness_readers_do_not_leak_between_accounts(self):
        self.profile_fixture();_,lid,_,_,unit=self.seed_unit();self.defer_fixture(unit,lid)
        original=self.token
        other_token=os.urandom(24).hex()
        with connect(self.db) as c:
            other=c.execute("INSERT INTO users(username,password_hash,timezone,created_at) VALUES(?,'unused','UTC','2026-10-01')",('isolation-'+other_token,)).lastrowid
            c.execute("INSERT INTO sessions VALUES(?,?,'2026-10-01','2027-01-01')",(hashlib.sha256(other_token.encode()).hexdigest(),other))
        for _ in range(4):
            self.client.cookies.set('cvt_session',original)
            self.assertEqual(self.call('GET','/api/proficiency/readiness')['summary']['deferred_units'],1)
            self.client.cookies.set('cvt_session',other_token)
            self.assertEqual(self.call('GET','/api/proficiency/readiness')['summary']['deferred_units'],0)
        self.client.cookies.set('cvt_session',original)

    def test_reinstall_does_not_restore_explicit_new_preference(self):
        self.profile_fixture()
        with connect(self.db) as c:
            c.execute("INSERT INTO settings VALUES(?,'frequency_skip_basic_600','true')",(self.uid,))
            c.execute("INSERT INTO settings VALUES(?,'skip_basic_600','false')",(self.uid,))
        subprocess.run([str(CTL),'assessment-install','--database',str(self.db),'--backup',str(self.directory/(self.token+'-new.db'))],check=True,capture_output=True)
        self.assertFalse(self.call('GET','/api/settings')['skip_basic_600'])

    def test_recognition_success_alone_does_not_pass_scope(self):
        self.profile_fixture();self.seed_unit()
        sess=self.call('POST','/api/proficiency/confirmations',{'band':1})
        for _ in range(12):
            if sess['state']!='active':break
            sess=self.answer(sess,correct=sess['question']['kind']=='recognition')
        self.assertEqual(sum(x['correct'] for x in sess['result']['bands']),8)
        self.assertFalse(sess['result']['passed'])
        self.call('POST',f"/api/assessments/{sess['id']}/apply",{},status=409)
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)

    def test_known_assessment_gap_survives_later_group_pass(self):
        self.profile_fixture();self.seed_unit()
        with connect(self.db) as c:
            c.execute("INSERT INTO assessment_responses(session_id,sequence,item_id,issued_at,answered_at,answer,correct,option_order) VALUES(?,1,'r-airport','2026-10-25T09:00:00+00:00','2026-10-25T09:00:00+00:00','',0,'[0,1,2,3]')",('fixture-'+self.token,))
        sess=self.call('POST','/api/proficiency/confirmations',{'band':1})
        for _ in range(12):
            if sess['state']!='active':break
            sess=self.answer(sess)
        self.assertTrue(sess['result']['passed'])
        self.call('POST',f"/api/assessments/{sess['id']}/apply",{})
        self.assertEqual(self.call('GET','/api/stats')['new_senses'],1)

    def test_first_upgrade_repairs_only_legacy_adaptive_baseline(self):
        self.profile_fixture()
        path=self.directory/(self.token+'-legacy.db')
        with connect(self.db) as source,connect(path) as target:source.backup(target)
        with connect(path) as c:
            c.execute('DROP TABLE readiness_schema')
            c.execute("INSERT INTO settings VALUES(?,'frequency_skip_basic_600','true')",(self.uid,))
            c.execute("INSERT INTO settings VALUES(?,'skip_basic_600','false')",(self.uid,))
        subprocess.run([str(CTL),'assessment-install','--database',str(path),'--backup',str(self.directory/(self.token+'-legacy-backup.db'))],check=True,capture_output=True)
        with connect(path) as c:self.assertEqual(c.execute("SELECT value FROM settings WHERE user_id=? AND key='skip_basic_600'",(self.uid,)).fetchone()[0],'true')
