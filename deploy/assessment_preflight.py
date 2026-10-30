"""Run against a server-local snapshot; never write the production database."""
import argparse,hashlib,http.cookiejar,json,os,sqlite3,subprocess,time
from pathlib import Path
from urllib.request import Request,build_opener,HTTPCookieProcessor
p=argparse.ArgumentParser();p.add_argument('--release',required=True);p.add_argument('--directory',required=True);p.add_argument('--source',default='/var/lib/englishlearning/vocabulary.db');p.add_argument('--port',type=int,default=18089);a=p.parse_args()
release=Path(a.release);directory=Path(a.directory);directory.mkdir(mode=0o700)
ctl=release/'bin/englishctl';db=directory/'copy.db'
def cli(*args):return json.loads(subprocess.check_output([str(ctl),*map(str,args)]))
cli('backup','--database',a.source,'--output',db)
def digest(path,tables=None):
 c=sqlite3.connect(f'file:{path}?mode=ro',uri=True);c.execute('BEGIN')
 tables=tables or [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
 out={}
 for name in tables:
  h=hashlib.sha256();count=0
  for row in c.execute('SELECT * FROM "'+name.replace('"','""')+'"'):
   h.update(json.dumps(row,ensure_ascii=False,separators=(',',':'),default=lambda v:v.hex()).encode());h.update(b'\n');count+=1
  out[name]=(count,h.hexdigest())
 c.close();return out
before=digest(db)
install=cli('assessment-install','--database',db,'--backup',directory/'before.db')
assert digest(db,before)==before,'Original tables changed during additive migration'
# The previous strict v22 CLI must continue to inspect the migrated copy.
old=Path('/opt/englishlearning/app/bin/englishctl')
subprocess.run([str(old),'inspect','--database',str(db)],check=True,stdout=subprocess.DEVNULL)
log=open(directory/'service.log','w');env={**os.environ,'REGISTRATION_CODE':'','COOKIE_SECURE':'false'}
proc=subprocess.Popen([str(release/'bin/english-server'),'--database',str(db),'--listen',f'127.0.0.1:{a.port}','--enable-api'],env=env,stdout=log,stderr=log)
client=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()));checks=[];timings=[]
def call(path,method='GET',body=None):
 t=time.monotonic();req=Request(f'http://127.0.0.1:{a.port}'+path,method=method,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
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
  q=sess['question'];assert 'answer' not in q and 'item_id' not in q
  with sqlite3.connect(db) as c:payload,order=c.execute('SELECT i.payload,r.option_order FROM assessment_responses r JOIN assessment_items i ON i.id=r.item_id WHERE r.session_id=? AND sequence=?',(sess['id'],q['sequence'])).fetchone()
  item=json.loads(payload)
  answer=str(json.loads(order).index(int(item['answer']))) if q['kind']=='recognition' else item['answer']
  body={'sequence':q['sequence'],'answer':answer}
  sess=call('/api/assessments/'+sess['id']+'/answers','POST',body)
  assert sess==call('/api/assessments/'+sess['id']+'/answers','POST',body),'Retry not idempotent'
 assert sess['state']=='completed' and sess['result']['sufficient']
 call('/api/assessments/'+sess['id']+'/apply','POST',{})
 profile=call('/api/proficiency');assert profile['mode']=='adaptive' and not profile['automatic_deferral']
 card=call('/api/next')['card'];assert card
 call('/api/stats')
 call('/api/review','POST',{'attempt_id':card['attempt_id'],'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],'learning_unit_id':card['learning_unit_id'],'user_answer':card['answer_form'],'active_response_ms':1500})
 call('/api/proficiency/mode','PATCH',{'mode':'frequency'})
 call('/api/stats')
 result={'passed':True,'original_tables_unchanged':len(before),'legacy_v22_cli_compatible':True,'bank_items':install['items'],'assessment_questions':sess['answered'],'checks':len(checks),'maximum_request_ms':max(timings),'production_mutated':False,'real_user_calibration':False,'automatic_deferral':False}
 print(json.dumps(result));(directory/'result.json').write_text(json.dumps(result,indent=2)+'\n')
finally:
 proc.terminate();proc.wait(timeout=15);log.close()
