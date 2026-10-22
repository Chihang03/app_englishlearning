"""Same-host HTTP parity/load comparison on two isolated production snapshots.
Only aggregate timings, hashes and status counts leave this script. No credentials/content.
Python is a comparison tool, never part of the Rust production release.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def frozen_python(args):
    os.environ['DATA_DIR']=str(args.database.parent)
    sys.path.insert(0,str(args.backend))
    from app.main import app
    import uvicorn
    moment=datetime.fromisoformat(args.clock)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
    for name in ['app.security','app.main','app.sense_learning','app.adaptive_memory','app.learning_calendar','app.passkeys']:
        module=__import__(name,fromlist=['datetime']);module.datetime=Clock
    uvicorn.run(app,host='127.0.0.1',port=args.port,lifespan='off',access_log=False,log_level='warning')


def request(port,token,path,method='GET',body=None,headers=None):
    data=None if body is None else json.dumps(body).encode()
    req=Request(f'http://127.0.0.1:{port}'+path,data=data,method=method,headers={'Cookie':'cvt_session='+token,'Content-Type':'application/json',**(headers or {})})
    begin=time.perf_counter()
    try:
        response=urlopen(req,timeout=40)
    except HTTPError as error:response=error
    except OSError:return 0,b'{}',{},(time.perf_counter()-begin)*1000
    with response:
        content=response.read();code=response.status;response_headers=dict(response.headers)
    return code,content,response_headers,(time.perf_counter()-begin)*1000


def metrics(pid):
    values={}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith(('VmRSS:','VmHWM:','Threads:')):values[line.split(':')[0]]=int(line.split()[1])
    stat=Path(f'/proc/{pid}/stat').read_text().split();values['cpu_seconds']=(int(stat[13])+int(stat[14]))/os.sysconf('SC_CLK_TCK')
    return values


def equivalent(a,b,path='response'):
    if isinstance(a,(int,float)) and not isinstance(a,bool) and isinstance(b,(int,float)):
        assert abs(a-b)<=1e-8*max(1,abs(a)),path
    elif isinstance(a,dict):
        assert isinstance(b,dict) and set(a)==set(b),path
        for key in a:
            if key!='attempt_id':equivalent(a[key],b[key],path+'.'+key)
    elif isinstance(a,list):
        assert isinstance(b,list) and len(a)==len(b),path
        for x,y in zip(a,b):equivalent(x,y,path+'[]')
    else:assert a==b,path


def compare(args):
    args.directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    database=args.directory/'snapshot.db'
    with closing(sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(database)) as target:source.backup(target)
    with closing(sqlite3.connect(database)) as c:
        uid=c.execute("SELECT u.id FROM users u LEFT JOIN review_history r ON r.user_id=u.id WHERE u.role='learner' GROUP BY u.id ORDER BY COUNT(r.id) DESC,u.id LIMIT 1").fetchone()[0]
    clock=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    token=secrets.token_urlsafe(32);children=[];servers=[];logs=[]
    results={'clock':clock,'reference_python':sys.version.split()[0],'source_opened_readonly':True,'samples':[],'parity_paths':[]}
    try:
        for kind in ['python','rust']:
            directory=args.directory/kind;directory.mkdir(mode=0o700);copy=directory/'vocabulary.db'
            with closing(sqlite3.connect(database)) as source,closing(sqlite3.connect(copy)) as target:source.backup(target)
            with closing(sqlite3.connect(copy)) as c:
                c.execute('INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,clock,'2099-01-01T00:00:00+00:00'));c.commit()
            with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            command=([sys.executable,str(Path(__file__).resolve()),'serve-python','--database',str(copy),'--backend',str(args.backend),'--clock',clock,'--port',str(port)] if kind=='python' else [str(args.native),'--database',str(copy),'--listen',f'127.0.0.1:{port}','--enable-api','--test-clock',clock])
            log=(directory/'service.log').open('wb');logs.append(log);start=time.perf_counter();child=subprocess.Popen(command,stdout=log,stderr=log,env={**os.environ,'RUST_LOG':'warn'});children.append(child)
            for _ in range(300):
                try:
                    if request(port,'','/api/health')[0]==200:break
                except OSError:pass
                if child.poll() is not None:raise RuntimeError(kind+' exited; inspect server-local log')
                time.sleep(.02)
            else:raise RuntimeError(kind+' readiness timeout')
            servers.append((kind,port,child));results.setdefault('startup',[]).append({'kind':kind,'seconds':time.perf_counter()-start,**metrics(child.pid)})
        def pair(path,method='GET',bodies=None):
            replies=[request(port,token,path,method,None if bodies is None else bodies[index]) for index,(_,port,_) in enumerate(servers)]
            assert replies[0][0]==replies[1][0],path+' status '+str([r[0] for r in replies])
            equivalent(json.loads(replies[0][1]),json.loads(replies[1][1]),path)
            results['parity_paths'].append(path);return [json.loads(reply[1]) for reply in replies]
        for path in ['/api/auth/me','/api/settings','/api/word-lists','/api/stats','/api/learning-calendar','/api/dictionary/address','/api/muted-words']:pair(path)
        cards=[response['card'] for response in pair('/api/next')]
        if all(cards):
            bodies=[{'word_id':c['id'],'sense_id':c['sense_id'],'example_id':c['example_id'],'learning_unit_id':c['learning_unit_id'],'attempt_id':c['attempt_id'],'user_answer':'__migration_wrong__','active_response_ms':1234} for c in cards]
            pair('/api/review','POST',bodies)
            pair('/api/study/hint','POST',[{'attempt_id':c['attempt_id'],'kind':'pronunciation'} for c in cards])
            pair('/api/study/related-exposure','POST',[{'attempt_id':c['attempt_id']} for c in cards])
            pair('/api/study/meanings','POST',[{'attempt_id':c['attempt_id'],'record_family_exposure':True} for c in cards])
            for body,card in zip(bodies,cards):body['user_answer']=card['answer_form']
            pair('/api/review','POST',bodies);pair('/api/stats');pair('/api/learning-calendar')
        catalogues=[request(port,token,'/api/study/meaning-catalog') for _,port,_ in servers]
        assert catalogues[0][0]==catalogues[1][0]==200 and catalogues[0][1]==catalogues[1][1],'catalogue byte parity'
        results['catalogue_bytes']=len(catalogues[0][1]);results['catalogue_sha256']=hashlib.sha256(catalogues[0][1]).hexdigest()
        results['catalogue_cold_http']=[{'kind':kind,'elapsed_ms':reply[3],**metrics(child.pid)} for (kind,_,child),reply in zip(servers,catalogues)]
        paths=['/api/settings','/api/word-lists','/api/stats','/api/learning-calendar','/api/dictionary/address']
        for concurrency in [1,5,20]:
            # Sequential contenders avoid simultaneous CPU competition; alternate order each stage.
            for kind,port,child in (servers if concurrency!=5 else servers[::-1]):
                before=metrics(child.pid);start=time.perf_counter()
                with ThreadPoolExecutor(max_workers=concurrency) as pool:replies=list(pool.map(lambda index:request(port,token,paths[index%len(paths)]),range(args.requests)))
                elapsed=time.perf_counter()-start;latencies=sorted(reply[3] for reply in replies);success=sorted(reply[3] for reply in replies if reply[0]==200);after=metrics(child.pid)
                results['samples'].append({'kind':kind,'concurrency':concurrency,'requests':args.requests,'statuses':dict(Counter(reply[0] for reply in replies)),'elapsed_seconds':elapsed,'requests_per_second':args.requests/elapsed,'successful_requests_per_second':len(success)/elapsed,'successful_p50_ms':success[(len(success)-1)//2] if success else None,'successful_p95_ms':success[int((len(success)-1)*.95)] if success else None,'p50_ms':latencies[(len(latencies)-1)//2],'p95_ms':latencies[int((len(latencies)-1)*.95)],'cpu_seconds':after['cpu_seconds']-before['cpu_seconds'],**{k:v for k,v in after.items() if k!='cpu_seconds'}})
        results['parity']=True
        (args.directory/'result.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results))
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=40)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for log in logs:log.close()


def rollback(args):
    # Exercise native -> legacy -> native on one isolated database; never restore an old snapshot.
    token='rust-rollback-isolated-test'
    with closing(sqlite3.connect(args.database)) as c:
        uid=c.execute("SELECT id FROM users WHERE role='learner' ORDER BY id LIMIT 1").fetchone()[0]
        c.execute("INSERT OR REPLACE INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),uid,args.clock,'2099-01-01T00:00:00+00:00'));c.commit()
        before={table:c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['review_history','sense_srs_state','adaptive_memory','sense_exposures']}
    replies=[]
    for index,kind in enumerate(['rust','python','rust']):
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        command=([str(args.native),'--database',str(args.database),'--listen',f'127.0.0.1:{port}','--enable-api','--test-clock',args.clock] if kind=='rust' else [sys.executable,str(Path(__file__).resolve()),'serve-python','--backend',str(args.backend),'--database',str(args.database),'--port',str(port),'--clock',args.clock])
        with (args.database.parent/f'rollback-{index}.log').open('wb') as log:
            child=subprocess.Popen(command,stdout=log,stderr=log,env={**os.environ,'RUST_LOG':'warn'})
            try:
                for _ in range(300):
                    try:
                        if request(port,'','/api/health')[0]==200:break
                    except OSError:pass
                    if child.poll() is not None:raise RuntimeError('rollback service exited')
                    time.sleep(.02)
                values=[]
                for path in ['/api/auth/me','/api/settings','/api/stats','/api/learning-calendar']:
                    response=request(port,token,path);assert response[0]==200,'rollback '+path;values.append(json.loads(response[1]))
                replies.append(values)
            finally:
                child.terminate()
                try:child.wait(timeout=40)
                except subprocess.TimeoutExpired:child.kill();child.wait()
    equivalent(replies[0],replies[1]);equivalent(replies[1],replies[2])
    sys.path.insert(0,str(args.backend))
    from fsrs import Card
    with closing(sqlite3.connect(args.database)) as c:
        after={table:c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}
        assert before==after,'rollback lost learning rows'
        parsed=sum(1 for row in c.execute('SELECT card_json FROM adaptive_memory') if Card.from_json(row[0]))
    report={'sequence':['rust','python','rust'],'same_database':True,'response_parity':True,'learning_counts_preserved':True,'legacy_cards_parsed':parsed}
    (args.database.parent/'rollback-result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for name in ['compare','serve-python','rollback']:
        p=sub.add_parser(name);p.add_argument('--database',type=Path,required=True);p.add_argument('--backend',type=Path,required=True)
        if name=='compare':p.add_argument('--native',type=Path,required=True);p.add_argument('--directory',type=Path,required=True);p.add_argument('--requests',type=int,default=120)
        elif name=='serve-python':p.add_argument('--clock',required=True);p.add_argument('--port',type=int,required=True)
        else:p.add_argument('--clock',required=True);p.add_argument('--native',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='compare':compare(args)
    elif args.command=='rollback':rollback(args)
    else:frozen_python(args)
if __name__=='__main__':main()
