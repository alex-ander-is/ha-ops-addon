/* Qualified Linux-backed discovery browser proof. No production activation. */
import { spawn } from 'node:child_process';
import { mkdirSync,writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath,pathToFileURL } from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../..');
const shared='/Users/purportex/Applications/Playwright';
const {chromium}=await import(pathToFileURL(path.join(shared,'src/runtime.mjs')));
const artifacts=process.env.HA_OPS_BROWSER_ARTIFACTS_DIR||`/private/tmp/ha-ops-folder-scope-${Date.now()}`;
mkdirSync(artifacts,{recursive:true});
const owned=[],ids=[],errors=[];let child,context;
const bounded=async(p,label,ms=15000)=>{let timer;try{return await Promise.race([p,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label+' timed out')),ms);})]);}finally{clearTimeout(timer);}};
const assert=(value,message)=>{if(!value)throw Error(message);};
const json=async(url,init)=>{const response=await fetch(url,{...init,signal:AbortSignal.timeout(12000)});assert(response.ok,`HTTP ${response.status}`);return response.json();};
try{
  child=spawn('python3',[path.join(here,'folder_scope_fixture.py')],{cwd:repo,stdio:['ignore','pipe','inherit']});
  const fixture=await bounded(new Promise((resolve,reject)=>{let buffer='';child.stdout.on('data',chunk=>{buffer+=chunk;const line=buffer.split('\n').find(line=>line.startsWith('{'));if(line)resolve(JSON.parse(line));});child.once('exit',code=>reject(Error('Fixture exited '+code)));}),'qualified Linux fixture',70000);
  const base=fixture.baseUrl;
  const existing=process.env.HA_OPS_BROWSER_CDP_URL?await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL):null;
  context=existing?existing.contexts()[0]:await chromium.launchPersistentContext(path.join(shared,'user-data/google-chrome'),{channel:'chrome',headless:false,viewport:{width:1440,height:1000},args:['--profile-directory=Default','--remote-debugging-port=9227']});
  // A fresh owned page avoids borrowing any user or peer page.
  const page=await context.newPage();owned.push(page);
  const session=await context.newCDPSession(page);ids.push((await session.send('Target.getTargetInfo')).targetInfo.targetId);await session.detach().catch(error=>{if(!String(error).includes("No session with given id"))throw error;});
  writeFileSync(path.join(artifacts,'ownership.json'),JSON.stringify({fixture,runnerPid:process.pid,ownedTabIds:ids},null,2));
  page.on('pageerror',error=>errors.push(error.message));
  const mutations=[];page.on('request',request=>{if(request.method()==='POST')mutations.push({url:request.url(),body:request.postData()});});
  await page.goto(base);await page.locator('ha-ops-folder-scope vaadin-checkbox').first().waitFor();
  const component=page.locator('ha-ops-folder-scope');
  const folder=label=>component.locator('vaadin-checkbox').filter({has:page.locator('label',{hasText:label})});
  const checkbox=label=>component.getByRole('checkbox',{name:label,exact:true});
  const settled=()=>page.waitForFunction(()=>{const p=document.querySelector('ha-ops-folder-scope');return p&&!p.busy;});
  const model=()=>component.evaluate(p=>structuredClone(p.model));
  let state=await model();assert(state.policy.rules.length===0,'first install must be unchecked');assert((await component.getByRole('button').allTextContents()).every(label=>!label.toLowerCase().includes('check all')),'no Check All');
  const auth=page.locator('ha-ops-direct');
  await auth.getByLabel('Direct address',{exact:true}).fill('http://127.0.0.1:8100');
  assert(await auth.getByRole('button',{name:'Allow this browser',exact:true}).isEnabled(),'independent auth entry enabled with explicit address');
  assert(await component.getByText('secrets.yaml stays excluded.',{exact:true}).count()>0,'secret exclusion is readable');
  assert(await component.getByText(/remain selectable and can grow quickly/).count()>0,'runtime warning is advisory');
  const disabled=component.getByRole('button',{name:'Confirm discovery policy',exact:true});
  assert(await disabled.isDisabled(),'confirm initially disabled');
  const disabledStyle=await disabled.evaluate(e=>({background:getComputedStyle(e).backgroundColor,color:getComputedStyle(e).color,border:getComputedStyle(e).borderColor}));
  assert(disabledStyle.background==='rgb(238, 238, 238)'&&disabledStyle.border==='rgb(213, 217, 221)','visible disabled style');
  await checkbox('homeassistant').click();await settled();
  state=await model();assert(state.policy.rules.length===1,'parent inclusion persisted');
  await component.locator('vaadin-details').filter({hasText:'Subfolders of homeassistant'}).first().locator('summary').click().catch(async()=>{await component.locator('vaadin-details').first().evaluate(e=>{e.opened=true;});});
  await checkbox('B').waitFor();await checkbox('B').click();await settled();
  assert(await checkbox('homeassistant').evaluate(e=>e.getAttribute('aria-checked')==='mixed'||e.indeterminate||e.closest('vaadin-checkbox')?.indeterminate),'parent mixed state');
  // Expand B, then Space reincludes its C subtree without losing parent files.
  await component.locator('vaadin-details').evaluateAll(details=>details.forEach(e=>{e.opened=true;}));
  await checkbox('C').focus();await page.keyboard.press('Space');await settled();
  state=await model();assert(state.policy.rules.length===3,'nested exclude/reinclude persisted');
  const authority=state.auth_digest;
  writeFileSync(path.join(artifacts,'desktop-dom.json'),JSON.stringify(await component.evaluate(p=>({html:p.shadowRoot.innerHTML,model:p.model,checkboxes:[...p.shadowRoot.querySelectorAll('vaadin-checkbox')].map(c=>({label:c.label,checked:c.checked,indeterminate:c.indeterminate,nativeChecked:c.inputElement.checked,nativeMixed:c.inputElement.indeterminate}))})),null,2));
  await page.screenshot({path:path.join(artifacts,'desktop.png'),fullPage:true});
  const before=JSON.stringify(state.policy);
  await json(base+'fixture/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  await page.reload();await component.locator('vaadin-checkbox').first().waitFor();state=await model();
  assert(JSON.stringify(state.policy)===before,'restart and reload retain policy');assert(state.selected_files.length===0&&state.consent===null,'no file selection/consent hydration');assert(state.auth_digest===authority,'auth store independent');
  assert(await component.getByRole('button',{name:'Confirm discovery policy',exact:true}).isDisabled(),'reload expires policy consent');
  await component.getByRole('button',{name:'Review current discovery policy',exact:true}).click();await settled();
  assert(await component.getByRole('button',{name:'Confirm discovery policy',exact:true}).isEnabled(),'explicit re-review renews only policy confirmation');
  await component.getByRole('button',{name:'Confirm discovery policy',exact:true}).click();await settled();state=await model();assert(!state.pending,'re-reviewed same pending policy confirmed');
  const confirmedRules=JSON.stringify(state.policy.rules);
  await checkbox('homeassistant').click();await settled();
  await component.getByRole('button',{name:'Cancel discovery changes',exact:true}).click();await settled();state=await model();assert(JSON.stringify(state.policy.rules)===confirmedRules,'cancel restores prior confirmed policy');
  await page.locator('ha-ops-direct').getByRole('button',{name:'Set up later',exact:true}).click();assert(await page.locator('ha-ops-direct').getByRole('button',{name:'Allow this browser',exact:true}).isVisible(),'auth remains after cancellation/later');
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(artifacts,'mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile horizontal bounds');
  await page.goto(base+'legacy/');await page.locator('[data-testid="managed-targets-section"]').waitFor();assert(await page.locator('ha-ops-folder-scope').count()===0,'legacy gate');
  assert(mutations.every(request=>request.url.endsWith('/api/v1/discovery')&&!/token|authorization|apply|save/i.test(request.body||'')),'scope requests contain no content/auth dispatch');
  assert(errors.length===0,errors.join('\n'));
  writeFileSync(path.join(artifacts,'result.json'),JSON.stringify({status:'passed',disabledStyle,mutations,ownedTabIds:ids,authUnchanged:true,source:fixture.source},null,2));
  console.log(JSON.stringify({status:'passed',artifacts,ownedTabIds:ids}));
}finally{
  // Only this runner's recorded page and fixture are retired; shared profile and
  // all pre-existing tabs remain available to their owners.
  for(const page of owned)if(!page.isClosed())await bounded(page.close(),'owned tab close');
  if(child&&child.exitCode===null){const exited=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await bounded(exited,'owned fixture cleanup',25000);}
}

process.exit(process.exitCode||0);
