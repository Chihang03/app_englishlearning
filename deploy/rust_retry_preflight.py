"""Retry-aware overload validation on existing isolated snapshots, no production writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import argparse,hashlib,json,os,socket,sqlite3,subprocess,sys,time
from rust_http_preflight import request,metrics
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--backend',type=Path,required=True);parser.add_argument('--native',type=Path,required=True)
args=parser.parse_args();root=args.directory
clock=json.loads((root/'result.json').read_text())['clock'];token='isolated-retry-load';samples=[]
for kind in ['python','rust']:
 db=root/kind/'vocabulary.db'
 with closing(sqlite3.connect(db)) as c:
  c.execute('INSERT OR REPLACE INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,1,?,?)',(hashlib.sha256(token.encode()).hexdigest(),clock,'2099-01-01T00:00:00+00:00'));c.commit()
 with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
 cmd=([sys.executable,str(Path(__file__).with_name('rust_http_preflight.py')),'serve-python','--database',str(db),'--backend',str(args.backend),'--clock',clock,'--port',str(port)] if kind=='python' else [str(args.native),'--database',str(db),'--listen',f'127.0.0.1:{port}','--enable-api','--test-clock',clock])
 with (root/kind/'retry.log').open('wb') as log:
  child=subprocess.Popen(cmd,stdout=log,stderr=log,env={**os.environ,'RUST_LOG':'warn'})
  try:
   for _ in range(300):
    try:
     if request(port,'','/api/health')[0]==200:break
    except OSError:pass
    time.sleep(.02)
   paths=['/api/settings','/api/word-lists','/api/stats','/api/learning-calendar','/api/dictionary/address']
   for path in paths:assert request(port,token,path)[0]==200
   before=metrics(child.pid);start=time.perf_counter()
   def send(index):
    begin=time.perf_counter();retries=0
    while True:
     reply=request(port,token,paths[index%len(paths)])
     if reply[0]!=503 or retries>=10:return reply[0],(time.perf_counter()-begin)*1000,retries
     retries+=1;time.sleep(1+index*.003)
   with ThreadPoolExecutor(max_workers=20) as pool:replies=list(pool.map(send,range(60)))
   elapsed=time.perf_counter()-start;after=metrics(child.pid);latencies=sorted(r[1] for r in replies)
   samples.append({'kind':kind,'concurrency':20,'requests':60,'successful':sum(r[0]==200 for r in replies),'retries':sum(r[2] for r in replies),'elapsed_seconds':elapsed,'successful_rps':sum(r[0]==200 for r in replies)/elapsed,'p50_ms':latencies[29],'p95_ms':latencies[56],'VmHWM':after['VmHWM'],'cpu_seconds':after['cpu_seconds']-before['cpu_seconds']})
  finally:child.terminate();child.wait(timeout=40)
report={'max_retries':10,'retry_delay_seconds':'1.0 + request_index * 0.003','samples':samples}
(root/'retry-result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
