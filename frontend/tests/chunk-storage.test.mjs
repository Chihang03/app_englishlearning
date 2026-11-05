// Disposable-origin regression: interrupted blocks, delta updates and clearing.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const origin = process.env.APP_TEST_URL;
assert.ok(origin && ['localhost','127.0.0.1'].includes(new URL(origin).hostname));
const source = readFileSync(new URL('../src/studyContentCache.ts', import.meta.url),'utf8').replace('import { ApiError } from "./api";', 'class ApiError extends Error { constructor(status,message) { super(message); this.status=status; } }');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const browser=await chromium.launch({headless:true,channel:'chrome'});
try {
 const page=await browser.newPage(); await page.goto(origin+'/version.json');
 console.log(await page.evaluate(async source=>{
  const check=(v,m)=>{if(!v)throw new Error(m);};
  const {StudyContentCache}=await import(`data:text/javascript;base64,${btoa(unescape(encodeURIComponent(source)))}`);
  const create=async (id,word)=>{
   const text=JSON.stringify({words:[{id,word,senses:[],morphology_entries:[{lexical_unit_id:id,headword:word,pos_group:'noun',morphology:{inflected_forms:[{spelling:word+'s',label:'复数',form_types:['plural'],scope_sense_id:null}],derived_words:[],related_words:[]}}]}]});
   const bytes=new TextEncoder().encode(text); const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
   return {text,hash,bytes:bytes.length,words:1};
  };
  const a=await create(1,'apple'),b=await create(65,'bank'),c=await create(65,'changed');
  let blocks=[a,b], fail=b.hash, requests=[];
  window.fetch=async url=>{
   if(url.endsWith('format=chunks'))return new Response(JSON.stringify({format:1,words:2,blocks:blocks.map(({hash,bytes,words})=>({hash,bytes,words}))}),{headers:{ETag:'"'+blocks[1].hash+'"','X-Catalog-Format':'chunks'}});
   const hash=url.split('chunk=')[1];requests.push(hash);
   if(hash===fail)throw new Error('simulated disconnect');
   return new Response([a,b,c].find(v=>v.hash===hash).text);
  };
  let cache=new StudyContentCache(1), rejected=false;
  try{await cache.syncCatalog('v1',true);}catch{rejected=true;}
  check(rejected&&!await cache.savedWord(1),'incomplete download published');
  fail='';requests=[];cache=new StudyContentCache(1);await cache.syncCatalog('v1',true);
  check(requests.length===1&&requests[0]===b.hash,'completed block was downloaded again after restart');
  check((await cache.savedWord(1)).morphology_entries[0].morphology.inflected_forms[0].spelling==='apples','morphology lost');
  blocks=[a,c];requests=[];await cache.syncCatalog('v2',true);
  check(requests.length===1&&requests[0]===c.hash,'unchanged block downloaded');
  check((await cache.savedWord(65)).word==='changed','delta missing');
  const other=new StudyContentCache(2);check(!await other.savedWord(1),'account content leaked');
  await other.syncCatalog('v2',true);
  localStorage.setItem('cvt.test.pending','preserve');
  await cache.removeDownloads();
  check(!await cache.savedWord(1)&&(await cache.searchWords('')).length===0,'clear left dictionary');
  check((await other.savedWord(1)).word==='apple'&&localStorage.getItem('cvt.test.pending')==='preserve','clear affected other account or pending storage');
  requests=[];await cache.syncCatalog('v3',true);check(requests.length===2,'clear retained block payload');
  const db=await new Promise(resolve=>{const r=indexedDB.open('cvt-study-content-v1',4);r.onsuccess=()=>resolve(r.result);});
  await new Promise(resolve=>{const tx=db.transaction('catalog-blocks','readwrite');tx.objectStore('catalog-blocks').put({accountId:1,hash:a.hash,bytes:new Uint8Array([1]).buffer,savedAt:Date.now()});tx.oncomplete=resolve;});
  requests=[];await cache.syncCatalog('repair',true);
  check(requests.length===1&&requests[0]===a.hash,'corrupt local block not repaired');
  const originalPut=IDBObjectStore.prototype.put;
  let quota=true;
  IDBObjectStore.prototype.put=function(...args){
   if(this.name==='catalog-words'&&quota){quota=false;throw new DOMException('simulated quota','QuotaExceededError');}
   return originalPut.apply(this,args);
  };
  await cache.syncCatalog('quota',true);IDBObjectStore.prototype.put=originalPut;
  check(!quota&&(await cache.savedWord(1)).word==='apple','quota recovery lost published content');
  const normalFetch=window.fetch;
  let release,started;
  const pending=new Promise(resolve=>{started=resolve;});
  window.fetch=async url=>{if(url.endsWith('format=chunks')){started();await new Promise(resolve=>{release=resolve;});}return normalFetch(url);};
  const sync=cache.syncCatalog('clear-race',true);await pending;
  await new StudyContentCache(1).removeDownloads();release();await sync;
  check(!await cache.savedWord(1),'in-flight sync resurrected cleared dictionary');
  db.close();
  return 'PASS resume, delta, morphology, account isolation, targeted clear, corruption repair, quota recovery and concurrent clear';
 },compiled));
}finally{await browser.close();}
