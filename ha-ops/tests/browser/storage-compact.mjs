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
  const overhead=storage.locator('[data-testid="storage-overhead"]');
  const badge=storage.locator('[data-testid="storage-header"] ha-ops-status-badge');
  const closed=async()=>{if(await storage.locator('vaadin-details').count()){await storage.locator('vaadin-details').evaluate(e=>{e.opened=false;});assert(await storage.locator('vaadin-details').evaluate(e=>!e.opened),'details remain closed');}};
  assert(await overhead.isVisible()&&(await overhead.innerText())==='HA Ops overhead: Unknown','unknown header before first Calculate');
  await storage.getByRole('button',{name:'Calculate',exact:true}).click();await settled();
  await storage.getByText(/Last completed Calculate:/).waitFor();
  const actual=await storage.evaluate(e=>structuredClone(e.model));
  const actualPhysical=actual.measurement.categories.reduce((sum,row)=>sum+row.allocated_bytes,0);
  const physicalText=number=>`HA Ops overhead: ${number.toLocaleString()} bytes physical allocated`;
  await closed();assert(await overhead.isVisible()&&(await overhead.innerText())===physicalText(actualPhysical),'actual six-category physical total is visible while closed');
  assert(await badge.evaluate(e=>e.severity==='warning'&&e.label==='WARNING'),'successful actual Calculate retains backend low-disk warning');
  const backendCases=(await(await fetch(base+'fixture/header-cases')).json()).model.header_cases;
  const scansBefore=(await(await fetch(base+'fixture/status')).json()).model.snapshot_count;
  const baseMeasurement=structuredClone(actual.measurement);
  const six=baseMeasurement.categories.map((row,index)=>({...row,allocated_bytes:index+1,logical_bytes:100000+index}));
  const projected=async(model,total,low,severity=low?'warning':'idle')=>{
    await storage.evaluate(async(e,model)=>{e.model=model;await e.updateComplete;},model);await closed();
    assert(await overhead.isVisible()&&(await overhead.innerText())===(total===null?'HA Ops overhead: Unknown':physicalText(total)),'closed physical sum/zero/Unknown');
    assert(await badge.evaluate((e,severity)=>e.severity===severity,severity),'header severity priority');
    if(low&&severity!=='error')assert(await badge.evaluate(e=>e.label==='WARNING'),'low-disk header WARNING');
  };
  const report=(measurement,extra={})=>({...actual,measurement,attempt:{status:'completed'},maintenance:{status:'completed'},...extra});
  await projected(report(null),null,false);
  await projected(report({...baseMeasurement,categories:six.map(row=>({...row,allocated_bytes:0})),volumes:[{...baseMeasurement.volumes[0],warning:false}]}),0,false);
  for(const boundary of backendCases){
    await projected(report({...baseMeasurement,categories:six,volumes:[{...baseMeasurement.volumes[0],...boundary,available_bytes:boundary.available,allocated_bytes:99999999}]}),21,boundary.warning);
  }
  for(const flags of [[false,true],[true,false]]){
    await projected(report({...baseMeasurement,categories:six,volumes:flags.map((warning,index)=>({...baseMeasurement.volumes[0],volume:'fixture-'+index,warning,allocated_bytes:88888888}))}),21,true);
  }
  await projected(report({...baseMeasurement,categories:six,volumes:[{...baseMeasurement.volumes[0],available_bytes:0,warning:false}]}),21,false);
  for(const status of ['effect-started','uncertain'])await projected(report({...baseMeasurement,categories:six,volumes:[{...baseMeasurement.volumes[0],warning:false}]},{maintenance:{status}}),21,false,'warning');
  assert((await(await fetch(base+'fixture/status')).json()).model.snapshot_count===scansBefore,'header projections perform zero extra scans');
  await storage.evaluate(async(e,model)=>{e.model=model;await e.updateComplete;},actual);
  await storage.locator('vaadin-details').evaluate(e=>{e.opened=true;});
  await storage.getByText('Browser authorization overhead',{exact:true}).waitFor();
  await storage.getByText(/No pending operation/).waitFor();
  const timestamp=await storage.evaluate(e=>e.model.measurement.completed_at);
  for(const status of ['uncertain','unavailable','failed','incomplete']){
    const message='Latest attempt '+status;
    await storage.evaluate((element,{status,message})=>{element.model={...element.model,attempt:{status,error:message},maintenance:{status:'completed'}};},{status,message});
    await storage.getByText(new RegExp(message)).waitFor();
    assert(await badge.evaluate((e,status)=>e.severity===(['failed','unavailable'].includes(status)?'error':'warning'),status),'attempt severity overrides completed and low disk');
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
  await closed();assert(await overhead.isVisible()&&await badge.evaluate(e=>e.label==='WARNING'),'closed desktop header total and WARNING');
  const beforeLifecycle=(await(await fetch(base+'fixture/status')).json()).model.snapshot_count;
  await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile width');
  assert(await overhead.isVisible()&&(await overhead.innerText())===physicalText(actualPhysical)&&await badge.evaluate(e=>e.label==='WARNING'),'closed mobile total and low warning');
  await page.reload();await storage.getByRole('button',{name:'Calculate',exact:true}).waitFor();
  assert(await storage.evaluate(e=>e.model.measurement.completed_at)===timestamp,'measurement survives refresh');
  assert(await storage.evaluate(e=>e.review===null),'confirmation cleared on refresh');
  await closed();assert((await overhead.innerText())===physicalText(actualPhysical)&&await badge.evaluate(e=>e.label==='WARNING'),'refresh retains measured total and WARNING');
  await fetch(base+'fixture/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  await page.reload();await storage.getByRole('button',{name:'Calculate',exact:true}).waitFor();
  assert(await storage.evaluate(e=>e.model.measurement.completed_at)===timestamp,'measurement survives backend restart');
  const lifecycle=(await(await fetch(base+'fixture/status')).json()).model;
  assert(lifecycle.mutation_count===1,'lifecycle never repeats mutation');
  assert(lifecycle.snapshot_count===beforeLifecycle,'refresh/restart never scans');
  await closed();assert((await overhead.innerText())===physicalText(actualPhysical)&&await badge.evaluate(e=>e.label==='WARNING'),'restart retains measured total and WARNING');
  // Same-version backend restart changes the incarnation and expires review.
  await storage.getByRole('button',{name:'Compact',exact:true}).click();await settled();
  assert(await storage.evaluate(e=>Boolean(e.review)),'explicit review exists');
  await fetch(base+'fixture/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const restarted=(await (await fetch(base+'fixture/status')).json()).model;
  await page.evaluate(model=>{const app=document.querySelector('ha-ops-app');app.view={...app.view,native_storage:model};},restarted);
  await page.waitForFunction(()=>document.querySelector('ha-ops-storage').review===null);
  const beforeVersion=(await(await fetch(base+'fixture/status')).json()).model.snapshot_count;
  await page.evaluate(()=>document.querySelector('ha-ops-app').observeBackendVersion('fixture-updated'));
  await closed();assert((await overhead.innerText())===physicalText(actualPhysical)&&await badge.evaluate(e=>e.label==='WARNING'),'version update retains old measurement total and warning');
  assert(await storage.evaluate(e=>e.model.measurement.completed_at)===timestamp,'version update preserves timestamp');
  assert((await(await fetch(base+'fixture/status')).json()).model.snapshot_count===beforeVersion,'version update does not scan');
  for(const qualifier of [false,"true",{}]){
    await page.evaluate(qualified=>{const app=document.querySelector('ha-ops-app');app.view={...app.view,native_storage:{qualified}};},qualifier);
    await page.waitForFunction(()=>!document.querySelector('ha-ops-storage'));
  }
  await page.goto(base+'legacy/');await page.locator('[data-testid="managed-targets-section"]').waitFor();assert(await page.locator('ha-ops-storage').count()===0,'exact qualified gate');
  assert(errors.length===0,errors.join('\n'));
  writeFileSync(path.join(artifacts,'result.json'),JSON.stringify({status:'passed',source:fixture.source,ownedTabIds:ids,disabledStyle,mutationCount:1,measurementTimestamp:timestamp,physicalTotal:actualPhysical,backendWarningCases:backendCases,closedDetails:true,lifecycleSnapshotCount:beforeLifecycle},null,2));
  console.log(JSON.stringify({status:'passed',artifacts,ownedTabIds:ids}));
}catch(error){
  if(owned[0]&&!owned[0].isClosed()){await owned[0].screenshot({path:path.join(artifacts,'failure.png'),fullPage:true});writeFileSync(path.join(artifacts,'failure.txt'),await owned[0].locator('body').innerText());}
  throw error;
}finally{
  for(const page of owned)if(!page.isClosed())await bounded(page.close(),'owned tab cleanup');
  if(child&&child.exitCode===null){const exit=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await bounded(exit,'owned fixture cleanup',25000);}
}
process.exit(process.exitCode||0);
