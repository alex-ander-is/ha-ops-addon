/* Real rebuilt bundle + real Candidate/Review + actual protected adapters. */
import {spawn} from 'node:child_process';
import {mkdirSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
if(!process.env.HA_OPS_BROWSER_CDP_URL)throw Error('HA_OPS_BROWSER_CDP_URL must name the existing dedicated persistent-profile browser.');
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../..');
const artifacts=process.env.HA_OPS_BROWSER_ARTIFACTS_DIR;
if(!artifacts)throw Error('Set HA_OPS_BROWSER_ARTIFACTS_DIR outside product inputs');
mkdirSync(artifacts,{recursive:true});
const {chromium}=await import(pathToFileURL('/Users/purportex/Applications/Playwright/src/runtime.mjs'));
const child=spawn('/usr/bin/python3',[path.join(here,'migration_review_fixture.py'),...(process.argv.includes('--portable')?['--portable']:[])],{cwd:repo,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','pipe','pipe']});
let stderr='';child.stderr.on('data',b=>stderr+=b);
const owned=[],ids=[],errors=[],requests=[];
function assert(value,message){if(!value)throw Error(message);}
const bounded=(promise,label,ms=10000)=>Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(Error(label+' timed out: '+stderr)),ms))]);
try{
 const fixture=await bounded(new Promise((resolve,reject)=>{let buffer='';child.stdout.on('data',b=>{buffer+=b;const line=buffer.split('\n').find(x=>x.startsWith('{'));if(line)resolve(JSON.parse(line));});child.once('exit',code=>reject(Error('Fixture exited '+code+': '+stderr)));}),'fixture',120000);
 const portable=process.argv.includes('--portable');
 assert(fixture.source===(portable?'portable-worker-real-domain':'linux-real-domain'),'fixture qualification mismatch');
 assert(portable||fixture.worker_platform==='linux','Linux worker platform not verified');
 const browser=await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL);const context=browser.contexts()[0];
 async function pageFor(url){const page=await context.newPage();owned.push(page);const session=await context.newCDPSession(page);ids.push((await session.send('Target.getTargetInfo')).targetInfo.targetId);await session.detach();writeFileSync(path.join(artifacts,'ownership.json'),JSON.stringify({ownedTabIds:ids,source:fixture.source},null,2));page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/migration/'))requests.push({method:r.method(),path:new URL(r.url()).pathname});});await page.goto(url);return page;}
 // Consume the actual short-lived enrollment before expensive domain flows.
 // The fixture uses the unchanged Authority's 120-second one-use grant.
 const direct=await pageFor(fixture.directUrl);const directComponent=direct.locator('ha-ops-migration-review');
 const directReady=async label=>{
  const started=Date.now();
  try{
   await direct.waitForFunction(()=>{const parent=document.querySelector('ha-ops-direct');const child=parent?.shadowRoot.querySelector('ha-ops-migration-review');return parent?.snapshot&&child?.state&&!child.busy;},null,{timeout:120000});
  }catch{
   const diagnostic=await direct.evaluate(()=>{const parent=document.querySelector('ha-ops-direct');const child=parent?.shadowRoot.querySelector('ha-ops-migration-review');return {parentPresent:!!parent,message:String(parent?.message||'').replace(/https?:\/\/\S+/g,'[url]').replace(/[A-Za-z0-9_-]{32,}/g,'[redacted]'),hasSnapshot:!!parent?.snapshot,childPresent:!!child,childHasState:!!child?.state,childStatus:child?.state?.status||null,childBusy:!!child?.busy};});
   writeFileSync(path.join(artifacts,'direct-readiness-failure.json'),JSON.stringify({label,elapsedMilliseconds:Date.now()-started,...diagnostic},null,2));
   await direct.screenshot({path:path.join(artifacts,'direct-readiness-failure.png'),fullPage:true});
   throw Error('Direct authenticated readiness failed; see sanitized direct-readiness-failure artifacts');
  }
 };
 await directReady('initial enrollment');
 assert(await directComponent.getByRole('button',{name:'Approve exact candidate and scope',exact:true}).isDisabled(),'initial direct authentication restored approval');
 const page=await pageFor(fixture.baseUrl);const component=page.locator('ha-ops-migration-review');
 const settled=()=>page.waitForFunction(()=>{const e=document.querySelector('ha-ops-migration-review');return e?.state&& !e.busy;},null,{timeout:120000});
 await settled();assert(await component.locator('[data-testid=migration-row]').count()>0,'complete paths absent');
 const approve=component.getByRole('button',{name:'Approve exact candidate and scope',exact:true});
 assert(await approve.isDisabled(),'auth/read must not approve');
 const disabledStyle=await approve.evaluate(e=>({background:getComputedStyle(e).backgroundColor,border:getComputedStyle(e).borderColor}));
 assert(disabledStyle.background==='rgb(238, 238, 238)'&&disabledStyle.border==='rgb(213, 217, 221)','disabled style');
 const reviewButton=component.getByRole('button',{name:'Review current candidate',exact:true});
 for(const status of ['stale','unavailable']){
  const stateRoute='**/api/v1/migration/state';
  await page.route(stateRoute,async route=>{const response=await route.fetch();const body=await response.json();await route.fulfill({response,json:{...body,status}});});
  await component.evaluate(e=>e.load());await settled();
  assert(await reviewButton.isDisabled(),status+' model left Review enabled');
  const before=requests.filter(x=>x.path.endsWith('/review')).length;
  await reviewButton.evaluate(e=>e.click());await component.evaluate(e=>e.review());
  assert(requests.filter(x=>x.path.endsWith('/review')).length===before,status+' Review sent request');
  const style=await reviewButton.evaluate(e=>({background:getComputedStyle(e).backgroundColor,border:getComputedStyle(e).borderColor,color:getComputedStyle(e).color}));
  assert(style.background==='rgb(238, 238, 238)'&&style.border==='rgb(213, 217, 221)'&&style.color==='rgb(139, 146, 153)',status+' disabled Review style');
  if(status==='unavailable')await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(artifacts,'review-'+status+'.png'),fullPage:true});
  await page.unroute(stateRoute);await component.evaluate(e=>e.load());await settled();
  assert(await reviewButton.isEnabled()&&await approve.isDisabled(),'fresh state did not restore only explicit Review');
 }
 await page.setViewportSize({width:1280,height:900});
 await component.evaluate(e=>e.invalidate('Connection changed'));
 assert(await reviewButton.isDisabled(),'reset retained Review readiness');
 const resetBefore=requests.filter(x=>x.path.endsWith('/review')).length;await component.evaluate(e=>e.review());
 assert(requests.filter(x=>x.path.endsWith('/review')).length===resetBefore,'reset method guard sent request');
 await component.evaluate(e=>e.load());await settled();
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();assert(await approve.isEnabled(),'explicit review did not enable decision');
 const binary=component.locator('article[data-testid=migration-row]').filter({hasText:'homeassistant/binary'});await binary.locator('vaadin-details').last().evaluate(e=>e.opened=true);await binary.getByRole('button',{name:'Inspect after bytes',exact:true}).click();await settled();
 await component.getByRole('button',{name:'Last byte page',exact:true}).click();await settled();
 assert((await component.getByTestId('migration-byte-range').textContent()).includes('EOF true'),'last byte/EOF inaccessible');
 assert((await component.getByTestId('migration-text').textContent()).includes('not valid UTF-8'),'malformed bytes lacked explicit text refusal');
 await component.getByRole('button',{name:'Close content',exact:true}).click();await settled();
 const utf8=component.locator('article[data-testid=migration-row]').filter({hasText:'homeassistant/utf8'});
 await utf8.locator('vaadin-details').last().evaluate(e=>e.opened=true);
 await utf8.getByRole('button',{name:'Inspect after bytes',exact:true}).click();await settled();
 assert((await component.getByTestId('migration-text').textContent()).includes('é'),'first byte page lost split UTF8');
 assert((await component.getByTestId('migration-text-range').textContent()).includes('Text bytes 0–16385'),'text byte range was not labeled');
 await component.getByRole('button',{name:'Next bytes',exact:true}).click();await settled();
 assert((await component.getByTestId('migration-text').textContent()).includes('é'),'next byte page lost leading split UTF8');
 await component.getByRole('button',{name:'Last byte page',exact:true}).click();await settled();
 assert((await component.getByTestId('migration-text').textContent()).includes('😀last'),'direct last page lost split four-byte UTF8');
 await component.getByRole('button',{name:'Previous bytes',exact:true}).click();await settled();
 assert((await component.getByTestId('migration-text').textContent()).includes('é'),'reverse byte navigation lost UTF8');
 await component.getByRole('button',{name:'Close content',exact:true}).click();await settled();

 await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});
 await component.getByRole('button',{name:'Dismiss for this page',exact:true}).click();await settled();
 const decisionsBeforeReload=requests.filter(x=>x.path.endsWith('/decision')).length;
 await page.reload();await settled();assert(await component.getByRole('button',{name:'Review current candidate',exact:true}).isVisible(),'dismissed requirement not restored');assert(await approve.isDisabled(),'reload restored grant');assert(requests.filter(x=>x.path.endsWith('/decision')).length===decisionsBeforeReload,'reload replayed consent');
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();await component.getByRole('button',{name:'Refuse migration',exact:true}).click();await settled();await page.reload();await settled();assert(await approve.isDisabled(),'refusal reload restored grant');
 await directReady('remembered initial enrollment');
 assert(await directComponent.getByRole('button',{name:'Approve exact candidate and scope',exact:true}).isDisabled(),'direct remembered auth authorized consent');
 await direct.reload();await directReady('remembered reload');
 // A real authenticated direct review and explicit approval may legitimately
 // exceed the listener's input-intake deadline; auth alone must still not consent.
 const directSettled=()=>direct.waitForFunction(()=>{const e=document.querySelector('ha-ops-direct')?.shadowRoot.querySelector('ha-ops-migration-review');return e?.state&&!e.busy;},null,{timeout:120000});
 await directSettled();
 const directApprove=directComponent.getByRole('button',{name:'Approve exact candidate and scope',exact:true});
 assert(await directApprove.isDisabled(),'direct reload restored operation authority');
 const directReviewStarted=Date.now();
 await directComponent.getByRole('button',{name:'Review current candidate',exact:true}).click();await directSettled();
 assert(await directComponent.evaluate(e=>!!e.reviewId),'normal direct review failed: '+await directComponent.evaluate(e=>e.message));
 await directApprove.click();await directSettled();
 assert(await directComponent.evaluate(e=>e.state.decision?.action==='approve'&&e.reviewId===null),'normal explicit direct approval failed');
 const directReviewApproveMilliseconds=Date.now()-directReviewStarted;
 await direct.reload();await directReady('post-decision reload');
 assert(await directApprove.isDisabled(),'direct diagnostic approval became a replayable grant');
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile overflow');
 assert(await page.evaluate(()=>!Object.prototype.polluted&&!window.__migrationXss),'unsafe text execution');
 const response=await page.request.post(fixture.closedUrl+'api/v1/migration/decision/apply',{data:{}});assert(response.status()===404,'production missing peer fell through');
 // Retained component loads must clear page-only dismissal without restoring authority.
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();
 await component.getByRole('button',{name:'Dismiss for this page',exact:true}).click();await settled();
 await component.evaluate(e=>e.load());await settled();
 assert(await component.evaluate(e=>!e.hiddenPane&&e.reviewId===null),'retained load kept dismissal or authority');
 assert(await component.getByRole('button',{name:'Review current candidate',exact:true}).isVisible(),'retained load hid requirement');
 // Hold the real successful response after server work; lifecycle reset must fence its continuation.
 async function delayedLifecycle(routeName,trigger){
  let held,ready;const reached=new Promise(resolve=>ready=resolve);
  const pattern='**/api/v1/migration/'+routeName;
  await page.route(pattern,async route=>{const response=await route.fetch();held={route,response};ready();});
  await trigger();await bounded(reached,'held '+routeName,120000);
  await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
  const delivered=page.waitForResponse(response=>response.url().endsWith('/migration/'+routeName));
  await held.route.fulfill({response:held.response});await (await delivered).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.unroute(pattern);
  assert(await component.evaluate(e=>e.reviewId===null&&e.content===null&&e.page===null),'late '+routeName+' revived review or bytes');
  assert(await approve.isDisabled(),'late '+routeName+' enabled approval');
  assert(await reviewButton.isDisabled(),'late '+routeName+' restored Review readiness');
 }
 await delayedLifecycle('review',()=>component.getByRole('button',{name:'Review current candidate',exact:true}).click());
 await component.evaluate(e=>e.load());await settled();
 assert(await reviewButton.isEnabled()&&await approve.isDisabled(),'fresh state after delayed Review did not restore only explicit Review');
 assert(await component.evaluate(e=>e.reviewId===null&&e.content===null&&e.page===null),'fresh state restored old review authority or selection');
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();
 await binary.locator('vaadin-details').last().evaluate(e=>e.opened=true);
 await delayedLifecycle('content-page',()=>binary.getByRole('button',{name:'Inspect after bytes',exact:true}).click());
 // Explicitly retire only this fixture's held exact view; no page load performs cleanup.
 await component.evaluate(e=>e.api('content-close',{}));
 await page.evaluate(()=>window.dispatchEvent(new Event('online')));await settled();
 assert(await component.evaluate(e=>!e.hiddenPane&&e.reviewId===null),'reconnect restored dismissal or authority');
 // Completed private bytes survive backend restart as diagnostics, never page authority.
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();
 await binary.locator('vaadin-details').last().evaluate(e=>e.opened=true);
 await binary.getByRole('button',{name:'Inspect after bytes',exact:true}).click();await settled();
 const oldView=await component.evaluate(e=>({view_id:e.content.view_id,size:e.content.size,sha256:e.content.sha256}));
 const decisionsBeforeRestart=requests.filter(x=>x.path.endsWith('/decision')).length;
 await page.request.post(fixture.baseUrl+'fixture/restart',{data:{}});
 await component.evaluate(e=>e.load());await settled();
 assert(await component.evaluate(e=>e.reviewId===null&&e.content===null&&e.page===null),'restart restored authority or selection');
 assert(requests.filter(x=>x.path.endsWith('/decision')).length===decisionsBeforeRestart,'restart replayed a decision');
 const oldRefRefused=await component.evaluate(async(e,old)=>{try{await e.api('content-page',{view_id:old.view_id,offset:0});return false;}catch{return true;}},oldView);
 assert(oldRefRefused,'backend restart accepted old view identity');
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();
 await binary.locator('vaadin-details').last().evaluate(e=>e.opened=true);
 await binary.getByRole('button',{name:'Inspect after bytes',exact:true}).click();await settled();
 await component.getByRole('button',{name:'Last byte page',exact:true}).click();await settled();
 assert(await component.evaluate((e,old)=>e.page.eof&&e.content.sha256===old.sha256&&e.content.size===old.size,oldView),'fresh explicit restart review lost exact binary EOF');
 await component.getByRole('button',{name:'Close content',exact:true}).click();await settled();
 await component.getByRole('button',{name:'Review current candidate',exact:true}).click();await settled();await page.request.post(fixture.baseUrl+'fixture/stale',{data:{}});await approve.click();await settled();assert(await approve.isDisabled(),'stale candidate approval available');
 assert(errors.length===0,errors.join('\n'));
 writeFileSync(path.join(artifacts,'result.json'),JSON.stringify({status:'passed',source:fixture.source,directReviewApproveMilliseconds,disabledStyle,requests,ownedTabIds:ids,workerPlatform:fixture.worker_platform,mandatoryLinuxQualified:fixture.source==='linux-real-domain'&&fixture.worker_platform==='linux'},null,2));console.log(JSON.stringify({status:'passed',source:fixture.source,artifacts,ownedTabIds:ids}));
}finally{
 for(const page of owned)if(!page.isClosed())await bounded(page.close(),'owned tab close');
 if(child.exitCode===null){const exited=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await bounded(exited,'owned fixture close',20000);}
}
process.exit(process.exitCode||0);
