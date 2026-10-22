"""External test driver for a clean-room Rust HTTP service; never installed as a runtime dependency."""
import argparse
from http.cookiejar import CookieJar
import json
from urllib.request import Request,build_opener,HTTPCookieProcessor
from urllib.parse import quote
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=18087);args=p.parse_args()
client=build_opener(HTTPCookieProcessor(CookieJar()));checks=[]
def call(path,method='GET',body=None):
 req=Request(f'http://127.0.0.1:{args.port}'+path,method=method,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':'http://localhost:5173'})
 with client.open(req,timeout=20) as response:
  assert response.status==200,path;result=json.load(response)
 checks.append(method+' '+path);return result
call('/api/auth/register','POST',{'username':'clean_probe','password':'clean-runtime-password'})
call('/api/auth/me');settings=call('/api/settings');call('/api/settings','PATCH',{'show_sentence_translation':True})
call('/api/word-lists');call('/api/dictionary/address');card=call('/api/next')['card'];assert card
call('/api/study/hint','POST',{'attempt_id':card['attempt_id'],'kind':'pronunciation'})
call('/api/study/related-exposure','POST',{'attempt_id':card['attempt_id']})
call('/api/study/meanings','POST',{'attempt_id':card['attempt_id']})
call('/api/review','POST',{'attempt_id':card['attempt_id'],'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],'learning_unit_id':card['learning_unit_id'],'user_answer':card['answer_form'],'active_response_ms':1234})
report=call('/api/content-reports','POST',{'attempt_id':card['attempt_id'],'category':'definition','details':'clean runtime validation'})
call('/api/stats');call('/api/learning-calendar')
call('/api/words/'+str(card['id'])+'/mute','POST');call('/api/muted-words');call('/api/muted-words/'+quote(card['word'],safe=''),'DELETE')
call('/api/push/config');call('/api/auth/passkeys/config');call('/api/auth/passkeys/register/options','POST',{'current_password':'clean-runtime-password'})
call('/api/auth/logout','POST');call('/api/auth/login','POST',{'username':'admin','password':'temporary-clean-runtime-password'})
call('/api/admin/overview');call('/api/admin/users');call('/api/admin/reports');detail=call('/api/admin/reports/'+str(report['id']))
call('/api/admin/reports/'+str(report['id']),'PATCH',{'status':'resolved','resolution_notes':'clean runtime verified'})
call('/api/admin/report-review/export');call('/api/auth/logout','POST')
print(json.dumps({'clean_api_passed':True,'checks':checks,'runtime_interpreter_required':False}))
