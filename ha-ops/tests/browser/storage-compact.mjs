/* Shipped Vaadin storage header against qualified disposable Linux Git. */
import {spawn} from 'node:child_process';
import {mkdirSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url)),repo=path.resolve(here,'../../..');
const shared='/Users/purportex/Applications/Playwright';
const {chromium}=await import(pathToFileURL(path.join(shared,'src/runtime.mjs')));
const artifacts=process.env.HA_OPS_BROWSER_ARTIFACTS_DIR||`/private/tmp/storage-compact-${Date.now()}`;
mkdirSync(artifacts,{recursive:true});const owned=[],ids=[],errors=[];let child,context;
const assert=(value,message)=>{if(!value)throw Error(message);};
const bounded=async(p,label,ms=15000)=>{let timer;try{return await Promise.race([p,new Promise((_,reject)=>timer=setTimeout(()=>reject(Error(label+' timed out')),ms))]);}finally{clearTimeout(timer);}};
try{
  child=spawn('python3',[path.join(here,'storage_compact_fixture.py')],{cwd:repo,stdio:['ignore','pipe','inherit']});
  const fixture=await bounded(new Promise((resolve,reject)=>{let buffer='';child.stdout.on('data',chunk=>{buffer+=chunk;const line=buffer.split('\n').find(line=>line.startsWith('{'));if(line)resolve(JSON.parse(line));});child.once('exit',code=>reject(Error('fixture exited '+code)));}),'Linux fixture',70000);
  const existing=process.env.HA_OPS_BROWSER_CDP_URL?await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL):null;
  context=existing?existing.contexts()[0]:await chromium.launchPersistentContext(path.join(shared,'user-data/google-chrome'),{channel:'chrome',headless:false,viewport:{width:1440,height:1000},args:['--profile-directory=Default','--remote-debugging-port=9227']});
  const page=await context.newPage();owned.push(page);const session=await context.newCDPSession(page);ids.push((await session.send('Target.getTargetInfo')).targetInfo.targetId);await session.detach();
  writeFileSync(path.join(artifacts,'ownership.json'),JSON.stringify({fixture,runnerPid:process.pid,ownedTabIds:ids},null,2));
  page.on('pageerror',error=>errors.push(error.message));
  const base=fixture.baseUrl;await page.goto(base);
  const storage=page.locator('ha-ops-storage');await storage.getByRole('button',{name:'Calculate',exact:true}).waitFor();
  assert(await storage.evaluate(e=>e.closest('.control-card')!==null),'header placement');
  const settled=()=>page.waitForFunction(()=>!document.querySelector('ha-ops-storage')?.busy);
  await storage.getByRole('button',{name:'Calculate',exact:true}).click();await settled();
  await storage.getByText(/Last completed Calculate:/).waitFor();
  await storage.locator('vaadin-details').evaluate(e=>{e.opened=true;});
  await storage.getByText('Browser authorization overhead',{exact:true}).waitFor();
  await storage.getByText(/No pending operation/).waitFor();
  const timestamp=await storage.evaluate(e=>e.model.measurement.completed_at);
  for(const status of ['uncertain','unavailable','failed','incomplete']){
    const message='Latest attempt '+status;
    await storage.evaluate((element,{status,message})=>{element.model={...element.model,attempt:{status,error:message},maintenance:{status:'completed'}};},{status,message});
    await storage.getByText(new RegExp(message)).waitFor();
    assert(await storage.locator('ha-ops-status-badge').evaluate(e=>['warning','error'].includes(e.severity)),'attempt warning overrides old completion');
  }

  await storage.getByRole('button',{name:'Compact',exact:true}).click();await settled();
  const confirm=page.getByRole('button',{name:'Confirm Compact',exact:true});await confirm.waitFor();
  assert(await page.getByText(/recompression may save little or increase storage/).count()>0,'savings uncertainty');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();await settled();
  assert((await (await fetch(base+'fixture/status')).json()).model.mutation_count===0,'cancel no effect');
  await storage.getByRole('button',{name:'Compact',exact:true}).click();await settled();
  await confirm.click();await settled();
  const complete=await (await fetch(base+'fixture/status')).json();assert(complete.model.mutation_count===1,'real Compact executes exactly once');
  assert(complete.model.maintenance.status==='completed','real backend completed');
  assert(complete.model.measurement.completed_at===timestamp,'no post-Compact automatic Calculate');
  // Delayed preview cannot reopen after cancellation. Capture computed styles
  // while the actual Vaadin buttons are disabled in the component root.
  await page.route('**/api/v1/storage',async route=>{const value=route.request().postDataJSON();if(value.action==='preview')await new Promise(resolve=>setTimeout(resolve,450));await route.continue();});
  await storage.getByRole('button',{name:'Compact',exact:true}).click();
  const calculate=storage.getByRole('button',{name:'Calculate',exact:true});assert(await calculate.isDisabled(),'repeat action disabled');
  const disabledStyle=await calculate.evaluate(e=>({background:getComputedStyle(e).backgroundColor,color:getComputedStyle(e).color,border:getComputedStyle(e).borderColor}));
  assert(disabledStyle.background==='rgb(238, 238, 238)'&&disabledStyle.border==='rgb(213, 217, 221)','independent gray disabled state');
  await storage.evaluate(e=>e.request('cancel'));await page.waitForTimeout(700);assert(await storage.evaluate(e=>e.review===null),'late reply cannot reopen confirmation');await page.unroute('**/api/v1/storage');
  await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile width');
  await page.reload();await storage.getByRole('button',{name:'Calculate',exact:true}).waitFor();
  assert(await storage.evaluate(e=>e.model.measurement.completed_at)===timestamp,'measurement survives refresh');
  assert(await storage.evaluate(e=>e.review===null),'confirmation cleared on refresh');
  await fetch(base+'fixture/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  await page.reload();await storage.getByRole('button',{name:'Calculate',exact:true}).waitFor();
  assert(await storage.evaluate(e=>e.model.measurement.completed_at)===timestamp,'measurement survives backend restart');
  assert((await (await fetch(base+'fixture/status')).json()).model.mutation_count===1,'lifecycle never repeats mutation');
  // Same-version backend restart changes the incarnation and expires review.
  await storage.getByRole('button',{name:'Compact',exact:true}).click();await settled();
  assert(await storage.evaluate(e=>Boolean(e.review)),'explicit review exists');
  await fetch(base+'fixture/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const restarted=(await (await fetch(base+'fixture/status')).json()).model;
  await page.evaluate(model=>{const app=document.querySelector('ha-ops-app');app.view={...app.view,native_storage:model};},restarted);
  await page.waitForFunction(()=>document.querySelector('ha-ops-storage').review===null);
  for(const qualifier of [false,"true",{}]){
    await page.evaluate(qualified=>{const app=document.querySelector('ha-ops-app');app.view={...app.view,native_storage:{qualified}};},qualifier);
    await page.waitForFunction(()=>!document.querySelector('ha-ops-storage'));
  }
  await page.goto(base+'legacy/');await page.locator('[data-testid="managed-targets-section"]').waitFor();assert(await page.locator('ha-ops-storage').count()===0,'exact qualified gate');
  assert(errors.length===0,errors.join('\n'));
  writeFileSync(path.join(artifacts,'result.json'),JSON.stringify({status:'passed',source:fixture.source,ownedTabIds:ids,disabledStyle,mutationCount:1,measurementTimestamp:timestamp},null,2));
  console.log(JSON.stringify({status:'passed',artifacts,ownedTabIds:ids}));
}catch(error){
  if(owned[0]&&!owned[0].isClosed()){await owned[0].screenshot({path:path.join(artifacts,'failure.png'),fullPage:true});writeFileSync(path.join(artifacts,'failure.txt'),await owned[0].locator('body').innerText());}
  throw error;
}finally{
  for(const page of owned)if(!page.isClosed())await bounded(page.close(),'owned tab cleanup');
  if(child&&child.exitCode===null){const exit=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await bounded(exit,'owned fixture cleanup',25000);}
}
process.exit(process.exitCode||0);
