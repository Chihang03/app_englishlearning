"""Run against a server-local snapshot; never write the production database."""
import argparse,hashlib,http.cookiejar,json,os,sqlite3,subprocess,time,datetime,secrets
from pathlib import Path
from urllib.request import Request,build_opener,HTTPCookieProcessor
p=argparse.ArgumentParser();p.add_argument('--release',required=True);p.add_argument('--directory',required=True);p.add_argument('--source',default='/var/lib/englishlearning/vocabulary.db');p.add_argument('--port',type=int,default=18089);a=p.parse_args()
release=Path(a.release);directory=Path(a.directory);directory.mkdir(mode=0o700)
ctl=release/'bin/englishctl';db=directory/'copy.db'
def cli(*args):return json.loads(subprocess.check_output([str(ctl),*map(str,args)]))
cli('backup','--database',a.source,'--output',db)
original_columns={}
def digest(path,tables=None):
 c=sqlite3.connect(f'file:{path}?mode=ro',uri=True);c.execute('BEGIN')
 tables=tables or [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
 out={}
 for name in tables:
  h=hashlib.sha256();count=0
  if name not in original_columns:original_columns[name]=[r[1] for r in c.execute('PRAGMA table_info("'+name+'")')]
  selected=','.join('"'+col.replace('"','""')+'"' for col in original_columns[name])
  for row in c.execute('SELECT '+selected+' FROM "'+name.replace('"','""')+'"'):
   h.update(json.dumps(row,ensure_ascii=False,separators=(',',':'),default=lambda v:v.hex()).encode());h.update(b'\n');count+=1
  out[name]=(count,h.hexdigest())
 c.close();return out
before=digest(db)
install=cli('assessment-install','--database',db,'--backup',directory/'before.db')
after=digest(db,before)
assert {k:v for k,v in before.items() if k!='settings'}=={k:v for k,v in after.items() if k!='settings'},'Original learning data changed during additive migration'
with sqlite3.connect(directory/'before.db') as old,sqlite3.connect(db) as new:
 old_settings={(u,k):v for u,k,v in old.execute('SELECT user_id,key,value FROM settings')}
 new_settings={(u,k):v for u,k,v in new.execute('SELECT user_id,key,value FROM settings')}
 repaired=[key for key in old_settings if old_settings[key]!=new_settings.get(key)]
 for user,key in repaired:
  assert key=='skip_basic_600' and old_settings[(user,key)]=='false' and new_settings[(user,key)]=='true'
  assert old_settings.get((user,'frequency_skip_basic_600'))=='true'
  assert old.execute('SELECT mode FROM user_proficiency_profiles WHERE user_id=?',(user,)).fetchone()[0]=='adaptive'
 assert old_settings.keys()==new_settings.keys()

# The previous strict v22 CLI must continue to inspect the migrated copy.
old=Path('/opt/englishlearning/app/bin/englishctl')
subprocess.run([str(old),'inspect','--database',str(db)],check=True,stdout=subprocess.DEVNULL)
log=open(directory/'service.log','w');env={**os.environ,'REGISTRATION_CODE':'','COOKIE_SECURE':'false'}
proc=subprocess.Popen([str(release/'bin/english-server'),'--database',str(db),'--listen',f'127.0.0.1:{a.port}','--enable-api','--test-clock',datetime.datetime.now(datetime.timezone.utc).isoformat()],env=env,stdout=log,stderr=log)
client=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()));checks=[];timings=[];probe_token=None
def call(path,method='GET',body=None):
 t=time.monotonic();req=Request(f'http://127.0.0.1:{a.port}'+path,method=method,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json',**({'Cookie':'cvt_session='+probe_token} if probe_token else {})})
 with client.open(req,timeout=30) as response:result=json.load(response)
 timings.append(round((time.monotonic()-t)*1000,2));checks.append(method+' '+path.rsplit('/',1)[-1]);return result
try:
 for _ in range(100):
  try:
   call('/api/health');break
  except Exception:
   if proc.poll() is not None:raise RuntimeError('Candidate exited; inspect service.log')
   time.sleep(.1)
 else:raise RuntimeError('Candidate did not become ready')
 call('/api/auth/register','POST',{'username':'assessment_preflight_'+str(int(time.time()))[-6:],'password':'snapshot-only-test-password','timezone':'Asia/Shanghai'})
 call('/api/settings');call('/api/stats');call('/api/word-lists')
 sess=call('/api/assessments','POST',{})
 assert sess==call('/api/assessments','POST',{}),'Active session was not resumed'
 for _ in range(40):
  if sess['state']!='active':break
  q=sess['question'];assert q['kind']=='recognition' and 'answer' not in q and 'item_id' not in q
  with sqlite3.connect(db) as c:payload,order=c.execute('SELECT i.payload,r.option_order FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND sequence=?',(sess['id'],q['sequence'])).fetchone()
  item=json.loads(payload)
  answer=str(json.loads(order).index(int(item['answer']))) if q['kind']=='recognition' else item['answer']
  body={'sequence':q['sequence'],'answer':answer}
  sess=call('/api/assessments/'+sess['id']+'/answers','POST',body)
  assert sess==call('/api/assessments/'+sess['id']+'/answers','POST',body),'Retry not idempotent'
 assert sess['state']=='completed' and sess['result']['sufficient'] and 32<=sess['answered']<=40
 assert sess['result']['recall']['answered']==0 and all(b['answered']>=4 for b in sess['result']['bands'])
 call('/api/assessments/'+sess['id']+'/apply','POST',{})
 profile=call('/api/proficiency');assert profile['mode']=='adaptive' and profile['automatic_deferral']
 card=call('/api/next')['card'];assert card
 call('/api/stats')
 call('/api/review','POST',{'attempt_id':card['attempt_id'],'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],'learning_unit_id':card['learning_unit_id'],'user_answer':card['answer_form'],'active_response_ms':1500})
 call('/api/proficiency/mode','PATCH',{'mode':'frequency'})
 call('/api/stats')
 # Real-content scope confirmation must reduce actual eligible tasks.
 before_scope=call('/api/stats')
 scope=call('/api/proficiency/confirmations','POST',{'band':1})
 for _ in range(12):
  if scope['state']!='active':break
  q=scope['question'];assert q['kind']=='recognition'
  with sqlite3.connect(db) as c:payload,order=c.execute('SELECT i.payload,r.option_order FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND sequence=?',(scope['id'],q['sequence'])).fetchone()
  item=json.loads(payload);answer=str(json.loads(order).index(int(item['answer']))) if q['kind']=='recognition' else item['answer']
  scope=call('/api/assessments/'+scope['id']+'/answers','POST',{'sequence':q['sequence'],'answer':answer})
 assert scope['result']['passed'] and scope['result']['recall']['answered']==0
 call('/api/assessments/'+scope['id']+'/apply','POST',{})
 after_scope=call('/api/stats');deferred=call('/api/proficiency/readiness')['summary']['deferred_units']
 assert deferred>0 and after_scope['new_senses']<before_scope['new_senses'],'Scope confirmation did not actually remove tasks'
 # The production account is exercised ONLY in this isolated snapshot.
 with sqlite3.connect(db) as c:
  owner=c.execute("SELECT id FROM users WHERE lower(username)=lower('example-learner')").fetchone()
  if owner:
   probe_token=secrets.token_hex(32)
   c.execute("INSERT INTO sessions VALUES(?,?,'2026-01-01','2027-12-31')",(hashlib.sha256(probe_token.encode()).hexdigest(),owner[0]))
 comparison=None
 if owner:
  call('/api/proficiency/mode','PATCH',{'mode':'frequency'});call('/api/settings','PATCH',{'skip_basic_600':True})
  baseline=call('/api/stats');call('/api/proficiency/mode','PATCH',{'mode':'adaptive'});adaptive=call('/api/stats')
  assert call('/api/settings')['skip_basic_600'] is True
  effective=call('/api/proficiency/readiness')['units']
  assert all(u['source']=='scope_confirmation' for u in effective)
  with sqlite3.connect(db) as c:
   scheduled={r[0] for r in c.execute('SELECT learning_unit_id FROM sense_srs_state WHERE user_id=? AND retired_at IS NULL',(owner[0],))}
  assert all(u['id'] not in scheduled for u in effective),'Level policy overrode scheduled reviews'

  assert adaptive['due_words']<=baseline['due_words'] and adaptive['new_senses']<=baseline['new_senses']
  comparison={'baseline_due':baseline['due_words'],'adaptive_due':adaptive['due_words'],'baseline_new_units':baseline['new_senses'],'adaptive_new_units':adaptive['new_senses']}
 result={'passed':True,'original_tables_unchanged':len(before),'legacy_v22_cli_compatible':True,'bank_items':install['items'],'assessment_questions':sess['answered'],'checks':len(checks),'maximum_request_ms':max(timings),'slowest_check':checks[timings.index(max(timings))],'production_mutated':False,'real_user_calibration':False,'automatic_deferral':True,'scope_deferred_units':deferred,'scope_new_before':before_scope['new_senses'],'scope_new_after':after_scope['new_senses'],'account_comparison':comparison,'repaired_legacy_preferences':len(repaired)}
 print(json.dumps(result));(directory/'result.json').write_text(json.dumps(result,indent=2)+'\n')
finally:
 proc.terminate();proc.wait(timeout=15);log.close()
