"""Build original assessment items, never infer CEFR or claim calibrated difficulty.

Definitions and answer groups are authored in recognition.tsv. Cued recall is a
separate L1->L2 task (initial and length supplied), not free productive vocabulary.
All forms of a headword share an exposure identity. No dictionary scrape needed.
"""
import argparse, hashlib, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'backend/data/assessment/recognition.tsv'
OUTPUT=ROOT/'rust/fixtures/assessment-bank.json'

def build():
    items=[]; seen=set(); groups=[0]*6
    for line in SOURCE.read_text().splitlines():
        if not line or line.startswith('#'): continue
        band, group=line.split('\t'); band=int(band); entries=[x.split('=',1) for x in group.split('|')]
        assert len(entries)==4 and len({x[1] for x in entries})==4
        groups[band]+=1
        for position,(word,meaning) in enumerate(entries):
            assert word not in seen,word
            seen.add(word)
            item={'id':f'r-{word}','word':word,'family':word,'band':band,'difficulty':band-2.5,
                  'kind':'recognition','prompt':word,'options':[x[1] for x in entries],
                  'answer':str(position),'meaning':meaning,'review_status':'authored','calibrated':False}
            items.append(item)
            # 20 independently answered cued-recall items per band. Do not put
            # the recognition and recall forms of a word in the same test.
            if groups[band]<=5:
                items.append({**item,'id':f'p-{word}','kind':'recall',
                    'prompt':f'{meaning} · {word[0]} 开头，{len(word)} 个字母',
                    'options':[],'answer':word})
    assert groups==[20]*6,groups
    assert len(items)==600
    # Do not put near-synonyms (e.g. recondite/abstruse) in the same question.
    # At higher bands, use semantically unrelated distractors from other sets.
    for band in (4,5):
        pool=[x for x in items if x['band']==band and x['kind']=='recognition']
        for index,item in enumerate(pool):
            item['options']=[item['meaning']]+[pool[(index+offset)%len(pool)]['meaning'] for offset in (7,23,47)]
            item['answer']='0'
    return {'version':'original-2026-10-v1','license':'Project-original; no external test items copied',
            'calibration':'authored-prior-only','items':items}

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--check',action='store_true');args=parser.parse_args()
    data=json.dumps(build(),ensure_ascii=False,indent=2)+'\n'
    if args.check: assert OUTPUT.read_text()==data,'Bank drift'
    else: OUTPUT.write_text(data)
    print(json.dumps({'items':600,'sha256':hashlib.sha256(data.encode()).hexdigest()}))
