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
        self.assertEqual(response.status_code,status,response.text)
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
        self.assertFalse(self.call('GET','/api/proficiency')['automatic_deferral'])
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
