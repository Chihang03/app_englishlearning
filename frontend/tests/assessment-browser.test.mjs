// Isolated mobile UI contracts. Run API integration separately on native Rust.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE??'playwright');
const origin=process.env.APP_TEST_URL;
assert.ok(['localhost','127.0.0.1'].includes(new URL(origin).hostname));
const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH});
try {
 const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,colorScheme:'dark',serviceWorkers:'block'});
 let sess=null,mode='frequency',applied=false,answerCalls=0,lose=true;
 const profile=()=>({enabled:true,mode,profile:applied?{}:null,session:sess?{id:sess.id,state:sess.state}:null,labels:['基础词汇','日常词汇','常用进阶','广泛阅读','高阶词汇','专业与低频词汇']});
 const result={label:'常用进阶',range:['日常词汇','广泛阅读'],sufficient:true,boundary:false,bands:[{band:0,answered:4,correct:4},{band:2,answered:10,correct:7}],recall:{answered:4,correct:3}};
 await context.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;let json={};
  if(path==='/api/auth/me')json={user:{id:1,username:'assessment-ui',role:'learner',timezone:'Asia/Shanghai'}};
  else if(path==='/api/proficiency')json=profile();
  else if(path==='/api/assessments'&&route.request().method()==='POST') {
   sess??={id:'mobile',state:'active',answered:0,maximum:40,question:{sequence:1,kind:'recognition',prompt:'opportunity',options:['机会','风险','优势','劣势']},result:null,applied:false};json=sess;
  }else if(path==='/api/assessments/mobile/answers'){
   answerCalls++;
   const body=route.request().postDataJSON();
   if(body.sequence===1){
    sess={...sess,answered:1,question:{sequence:2,kind:'recall',prompt:'阅读 · r 开头，4 个字母',options:[]}};
    if(lose){lose=false;return route.abort('failed');}
   }else sess={...sess,state:'completed',answered:28,question:null,result};
   json=sess;
  }else if(path==='/api/assessments/mobile/apply'){applied=true;mode='adaptive';sess={...sess,applied:true};json=sess;}
  else if(path==='/api/assessments/mobile')json=sess;
  else if(path==='/api/proficiency/mode'){mode=route.request().postDataJSON().mode;json=profile();}
  else if(path==='/api/settings')json={show_sentence_translation:false,skip_basic_600:true,selected_word_list_ids:[]};
  else if(path==='/api/word-lists')json={lists:[]};
  else if(path==='/api/study/meaning-catalog')json={words:[]};
  else if(path==='/api/stats')json=Object.fromEntries(['total_study_time_ms','today_completed_cards','due_words','total_learned','learning','new_words','mature','streak_days','today_independent_accuracy','lapse_words','mastered','learned_senses','new_senses','mastered_senses','legacy_unmapped_words'].map(k=>[k,0]));
  await route.fulfill({json});
 });
 const page=await context.newPage();page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error(e.message);});
 await page.goto(origin);
 const enter=async()=>{await page.getByRole('button',{name:'设置',exact:true}).click();await page.getByRole('button',{name:'词汇水平测验',exact:true}).click();};
 await enter();await page.getByRole('button',{name:'开始测验',exact:true}).click();
 await page.getByRole('button',{name:'机会',exact:true}).click();
 await page.getByRole('button',{name:'重试提交',exact:true}).click();
 await page.getByLabel('英文答案',{exact:true}).waitFor();assert.equal(answerCalls,2);
 await page.getByRole('button',{name:'退出并保留进度',exact:true}).click();
 await page.getByRole('button',{name:'词汇水平测验',exact:true}).click();
 const input=page.getByLabel('英文答案',{exact:true});await input.fill('read');
 await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true});
 assert.equal(answerCalls,2);
 for(const width of [320,390,430]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
 await page.screenshot({path:'/tmp/english-assessment-mobile-question.png',fullPage:true});
 await page.getByRole('button',{name:'提交',exact:true}).click();
 await page.getByRole('button',{name:'按此水平开始',exact:true}).click();
 await page.getByRole('button',{name:'已应用到学习',exact:true}).waitFor();
 await page.screenshot({path:'/tmp/english-assessment-mobile-result.png',fullPage:true});
 await page.getByRole('button',{name:'返回设置',exact:true}).click();
 await page.getByRole('combobox',{name:'学习方式',exact:true}).waitFor();
 assert.equal(await page.getByText('跳过基础 800 词',{exact:true}).count(),0);
 await page.getByRole('combobox',{name:'学习方式',exact:true}).selectOption('frequency');
 await page.getByText('跳过基础 800 词',{exact:true}).waitFor();
 assert.deepEqual(errors,[]);console.log('PASS mobile assessment: resume, lost-response retry, IME, application, mode and 320/390/430px layouts');
 await context.close();
}finally{await browser.close();}
