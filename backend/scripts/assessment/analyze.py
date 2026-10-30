"""Read-only item diagnostics and deterministic model simulations.

A model-consistent simulation checks implementation, not real-world validity.
Item fits use person-separated train/validation sets and leave-one-item-out
ability. They are diagnostic proposals; no file this command writes activates
automatic deferral or CEFR labels.
"""
import argparse,json,math,random,sqlite3,statistics
from pathlib import Path

def probability(theta,b):return .25+.75/(1+math.exp(b-theta))
def estimate(evidence):
    grid=[-6+i*.1 for i in range(121)]
    logs=[-.5*(t/2)**2+sum(math.log(probability(t,b) if y else 1-probability(t,b)) for b,y in evidence) for t in grid]
    m=max(logs);weights=[math.exp(x-m) for x in logs];total=sum(weights)
    mean=sum(t*w for t,w in zip(grid,weights))/total
    sd=math.sqrt(sum((t-mean)**2*w for t,w in zip(grid,weights))/total)
    return mean,sd

def simulate():
    rng=random.Random(20261030);results=[]
    for theta in (-3,-2,-1,0,1,2,3):
        errors=[];coverage=[]
        for _ in range(30):
            e=[]
            for i in range(36):
                mean,sd=estimate(e)
                b=(i%6-2.5) if i<12 else max(-2.5,min(2.5,mean+.5))
                e.append((b,rng.random()<probability(theta,b)))
            mean,sd=estimate(e);errors.append(mean-theta);coverage.append(abs(mean-theta)<=1.645*sd)
        results.append({'theta':theta,'subjects':30,'bias':statistics.mean(errors),'rmse':math.sqrt(statistics.mean(x*x for x in errors)),'normal90_coverage':statistics.mean(coverage)})
    return {'kind':'model-consistent-simulation','seed':20261030,'real_user_calibration':False,'results':results}

def analyze(path):
    c=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('BEGIN')
    rows=c.execute("SELECT ss.user_id,ss.id,r.item_id,r.correct,i.difficulty FROM assessment_sessions ss JOIN assessment_responses r ON r.session_id=ss.id JOIN assessment_items i ON i.id=r.item_id WHERE ss.state='completed' AND r.answered_at IS NOT NULL AND i.kind='recognition' ORDER BY ss.id,r.sequence").fetchall()
    sessions={};items={}
    for r in rows:sessions.setdefault(r['id'],[]).append(r)
    for group in sessions.values():
        for r in group:
            evidence=[(x['difficulty'],bool(x['correct'])) for x in group if x['item_id']!=r['item_id']]
            theta,_=estimate(evidence)
            items.setdefault(r['item_id'],[]).append((r['user_id'],theta,bool(r['correct']),r['difficulty']))
    output=[]
    for item,values in sorted(items.items()):
        train=[v for v in values if v[0]%5!=0];valid=[v for v in values if v[0]%5==0]
        people=len({v[0] for v in train});prior=values[0][3]
        row={'item_id':item,'responses':len(values),'train_participants':people,'validation_participants':len({v[0] for v in valid}),'prior_difficulty':prior,'status':'insufficient'}
        if people>=100 and len({v[0] for v in valid})>=30:
            def loss(b,vs):return -sum(math.log(max(1e-8,probability(t,b) if y else 1-probability(t,b))) for _,t,y,_ in vs)/len(vs)
            fit=min([-6+i*.1 for i in range(121)],key=lambda b:loss(b,train)+.01*(b-prior)**2)
            row.update(status='diagnostic-fit',proposed_difficulty=fit,validation_log_loss=loss(fit,valid),prior_validation_log_loss=loss(prior,valid))
        output.append(row)
    return {'kind':'read-only-item-diagnostics','sessions':len(sessions),'participants':len({r['user_id'] for r in rows}),'items':output,'activation':False}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database');p.add_argument('--simulate',action='store_true');p.add_argument('--output',required=True);a=p.parse_args()
    if not a.simulate and not a.database:p.error('Choose --simulate or --database')
    result=simulate() if a.simulate else analyze(a.database)
    path=Path(a.output)
    with path.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':str(path),'kind':result['kind']}))
