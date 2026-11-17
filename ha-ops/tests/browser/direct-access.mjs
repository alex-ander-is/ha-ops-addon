import {spawn} from 'node:child_process';
import {mkdtempSync,mkdirSync,rmSync,readFileSync,writeFileSync,readdirSync,existsSync,realpathSync} from 'node:fs';
import os from 'node:os';
import {randomUUID} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const enrollmentOnly=process.argv.includes('--enrollment-only');
const bridgeSelfTest=process.argv.includes('--bridge-self-test');
if(!bridgeSelfTest&&!process.env.HA_OPS_BROWSER_CDP_URL) throw Error('HA_OPS_BROWSER_CDP_URL must name the existing dedicated persistent-profile browser. This fixture does not own browser startup or unrelated tabs.');
const {chromium}=await import(pathToFileURL('/Users/purportex/Applications/Playwright/src/runtime.mjs'));
const root=mkdtempSync(path.join(os.tmpdir(),'ha-ops-direct-browser-'));
const artifacts=process.env.HA_OPS_BROWSER_ARTIFACTS_DIR;
if(!artifacts)throw Error('Set HA_OPS_BROWSER_ARTIFACTS_DIR outside shipped product inputs');
mkdirSync(artifacts,{recursive:true});
const repoRoot=path.resolve(here,'../../..');
const nonce=randomUUID();
const linux=enrollmentOnly ? null : spawn('python3',[path.join(repoRoot,'dev/virtualization/ha_ops_dev.py'),'run','--input','.', '--','python3','ha-ops/tests/browser/linux_bridge.py','/workspace/output',nonce],{cwd:repoRoot,stdio:['ignore','pipe','pipe']});
let linuxLog='';if(linux){linux.stdout.on('data',b=>linuxLog+=b);linux.stderr.on('data',b=>linuxLog+=b);}
let child,bridgeOutput;
let stderr='';
let context,browser,tlsSession;const owned=[];
function assert(v,m){if(!v)throw Error(m);}
async function enrollmentChecks(fixture, context, record) {
  const ingress=await record(await context.newPage());
  const cleanupRoutes=[], releaseGates=[];
  async function ownRoute(owner, pattern, action) {
    const tasks=[], errors=[];
    let removed=false;
    const handler=route=>{
      // Route callbacks never reject outside the awaited test/cleanup path.
      const task=Promise.resolve().then(()=>action(route)).catch(error=>errors.push(error));
      tasks.push(task);return task;
    };
    await owner.route(pattern,handler);
    const remove=async()=>{
      if(removed)return;
      await Promise.all(tasks);
      await owner.unroute(pattern,handler);
      removed=true;
      await Promise.all(tasks);
      if(errors.length)throw errors[0];
    };
    cleanupRoutes.push(remove);return remove;
  }
  function heldRequest() {
    let release, arrived;
    const gate=new Promise(resolve=>release=resolve);
    const started=new Promise(resolve=>arrived=resolve);
    releaseGates.push(release);
    return {handler:async route=>{arrived();await gate;await route.continue();},
      finish:async remove=>{await started;release();await remove();}};
  }
  let failure;
  try {
  if(process.env.HA_OPS_BROWSER_BASELINE_BUNDLE)await ownRoute(ingress,'**/assets/ha-ops.js',route=>route.fulfill({contentType:'text/javascript',body:readFileSync(process.env.HA_OPS_BROWSER_BASELINE_BUNDLE)}));
  const events=[];
  ingress.on('request',request=>{
    const pathname=new URL(request.url()).pathname;
    if(pathname.endsWith('/direct-access/setup'))events.push({kind:'setup'});
    if(pathname.endsWith('/direct-access/allow'))events.push({kind:'allow',...request.postDataJSON()});
  });
  await ingress.goto(fixture.ingress);
  const address=ingress.getByRole('textbox',{name:'Direct address'});
  const allow=ingress.getByRole('button',{name:'Allow this browser',exact:true});
  await address.fill(new URL(fixture.direct).origin);
  await ingress.waitForFunction(()=>!!document.querySelector('ha-ops-direct')?.setup);
  const expired=await (await ingress.request.post(fixture.ingress+'fixture/expire')).json();
  assert(expired.initial_nonce_rejected,'Fixture failed to expire the initial real Authority nonce');
  const sessionGate=heldRequest();
  const removeSessionGate=await ownRoute(context,fixture.direct+'api/session',sessionGate.handler);
  const opened=ingress.waitForEvent('popup');await allow.click();const direct=await record(await opened);
  await direct.waitForFunction(()=>document.querySelector('ha-ops-direct')?.renderRoot?.textContent.includes('Checking browser authorization'));
  assert(!await direct.getByText('Remembered browser access is active.',{exact:true}).count(),'Pending session falsely reports active access');
  assert(!await direct.getByTestId('direct-authorization-success').count()&&!await direct.getByTestId('direct-authorization-info').count(),'Pending session showed success or remembered-access guidance');
  await sessionGate.finish(removeSessionGate);
  await direct.getByTestId('native-diagnostics').waitFor();
  await direct.getByText('Remembered browser access is active.',{exact:true}).waitFor();
  const success=direct.getByTestId('direct-authorization-success');
  const guidance='You can close this tab. This browser will remain authorized. Bookmark this page to open direct access again.';
  await success.getByRole('heading',{name:'Direct access is ready',exact:true}).waitFor();
  assert(await success.locator('ha-ops-status-badge[severity=success]').getAttribute('label')==='Browser authorized','Confirmed session lacks success badge');
  await direct.getByTestId('direct-authorization-info').getByText(guidance,{exact:true}).waitFor();
  assert(direct.url()===fixture.direct,'Authorization grant fragment not cleared');
  assert(await direct.evaluate(()=>window.opener===null),'Direct popup retained its opener');
  assert(events.map(e=>e.kind).join(',')==='setup,setup,allow','Idle authorization did not refresh setup exactly once before one POST');
  await ingress.getByText('Authorization window opened. Check that window to confirm direct access.',{exact:true}).waitFor();
  if(process.env.HA_OPS_BROWSER_BASELINE_BUNDLE)throw Error('Baseline unexpectedly passed idle nonce regression');
  const count=()=>events.filter(e=>e.kind==='allow').length;
  const originalCount=count();let popupCount=0;ingress.on('popup',()=>popupCount++);
  for(const invalid of ['not an address','ftp://localhost','http://localhost/path','http://user:pass@localhost','http://localhost:0']) {
    await address.fill(invalid);await allow.click();
    await ingress.getByText('Enter an HTTP or HTTPS address with its configured host port.',{exact:true}).waitFor();
  }
  assert(count()===originalCount&&popupCount===0,'Invalid address opened a popup or sent authorization');
  await address.fill(new URL(fixture.direct).origin);
  await ingress.evaluate(()=>{window.fixtureOpen=window.open;window.open=()=>null;});
  await allow.click();await ingress.getByText('Popup blocked.',{exact:false}).waitFor();
  assert(count()===originalCount,'Blocked popup sent authorization');
  await ingress.evaluate(()=>{window.open=window.fixtureOpen;delete window.fixtureOpen;});
  async function refusal(endpoint) {
    const routePattern=fixture.ingress+'api/direct-access/'+endpoint;
    const removeRefusal=await ownRoute(ingress,routePattern,route=>route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({ok:false,message:'Fixture '+endpoint+' refusal.'})}));
    const next=ingress.waitForEvent('popup');await allow.click();const blank=await record(await next);
    await ingress.getByText('Fixture '+endpoint+' refusal. Try Allow this browser again.',{exact:true}).waitFor();
    await ingress.waitForFunction(()=>!document.querySelector('ha-ops-direct')?.busy);
    assert(blank.isClosed(),'Failed authorization left its blank popup open');
    await removeRefusal();
  }
  await refusal('setup');assert(count()===originalCount,'Denied setup sent an allow POST');
  await refusal('allow');
  const refusedNonce=events.at(-1).nonce;
  // Hold the fresh setup to check immediate reentry, captured address, and disabled styling.
  const setupGate=heldRequest();
  const removeSetupGate=await ownRoute(ingress,fixture.ingress+'api/direct-access/setup',setupGate.handler);
  const before=count();const retry=ingress.waitForEvent('popup');
  await ingress.locator('ha-ops-direct').evaluate(app=>{app.allow();app.allow();});
  const retried=await record(await retry);
  await ingress.waitForFunction(()=>document.querySelector('ha-ops-direct')?.busy);
  assert(await allow.isDisabled(),'Busy Allow control is enabled');
  const disabled=await allow.evaluate(button=>{const s=getComputedStyle(button);return {background:s.backgroundColor,color:s.color,border:s.borderTopColor};});
  assert(disabled.background==='rgb(238, 238, 238)'&&disabled.color==='rgb(139, 146, 153)'&&disabled.border==='rgb(213, 217, 221)','Disabled Vaadin control lacks explicit muted colors');
  await address.fill('http://changed.invalid:1234');
  await setupGate.finish(removeSetupGate);await retried.getByTestId('native-diagnostics').waitFor();
  assert(count()===before+1,'Busy reentry issued more than one authorization POST');
  assert(events.at(-1).address===new URL(fixture.direct).origin,'Midflight edit changed captured authorization address');
  assert(events.at(-1).nonce!==refusedNonce,'Explicit retry reused the refused nonce');
  await ingress.getByText('Authorization window opened. Check that window to confirm direct access.',{exact:true}).waitFor();
  // Diagnostics errors must preserve a confirmed session; failed session must clear old diagnostics.
  const removeStatusRefusal=await ownRoute(direct,fixture.direct+'api/status',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'Fixture diagnostics offline.'})}));
  await direct.locator('ha-ops-direct').evaluate(app=>app.loadDirect());
  await direct.getByText('Remembered browser access is active.',{exact:true}).waitFor();
  await direct.getByText('Diagnostics unavailable. Fixture diagnostics offline.',{exact:true}).waitFor();
  assert(await success.count()===1,'Diagnostics failure removed confirmed authorization success');
  await direct.getByTestId('direct-authorization-info').getByText(guidance,{exact:true}).waitFor();
  assert(!await direct.getByTestId('native-diagnostics').count(),'Stale diagnostics retained after status failure');
  assert(!await direct.getByText('Allow this browser from the HA Ops page in Home Assistant.',{exact:false}).count(),'Confirmed direct session incorrectly requests enrollment after diagnostics fail');
  await direct.getByText('Direct access is authorized. Diagnostics will retry automatically.',{exact:true}).waitFor();
  await removeStatusRefusal();
  await direct.locator('ha-ops-direct').evaluate(app=>app.loadDirect());await direct.getByTestId('native-diagnostics').waitFor();
  const removeSessionRefusal=await ownRoute(direct,fixture.direct+'api/session',route=>route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({message:'Fixture session refused.'})}));
  await direct.locator('ha-ops-direct').evaluate(app=>app.loadDirect());
  await direct.getByText('Fixture session refused.',{exact:true}).waitFor();
  assert(!await direct.getByText('Remembered browser access is active.',{exact:true}).count()&&!await direct.getByTestId('native-diagnostics').count(),'Failed session retained active state or stale diagnostics');
  assert(!await direct.getByTestId('direct-authorization-success').count()&&!await direct.getByTestId('direct-authorization-info').count(),'Failed session retained success or remembered-access guidance');
  const successDOM=await direct.locator('ha-ops-direct').evaluate(component=>({success:!!component.renderRoot.querySelector('[data-testid=direct-authorization-success]'),info:!!component.renderRoot.querySelector('[data-testid=direct-authorization-info]')}));
  await removeSessionRefusal();
  const failed=await record(await context.newPage());
  await ownRoute(failed,fixture.direct+'bootstrap',route=>route.fulfill({status:403,contentType:'application/json',body:'{"ok":false}'}));
  await failed.goto(fixture.direct+'#grant=fixture-invalid-grant');
  await failed.getByText('Authorization expired. Allow this browser from Home Assistant again.',{exact:true}).waitFor();
  assert(!await failed.getByText('Remembered browser access is active.',{exact:true}).count(),'Failed bootstrap reported active access');
  assert(!await failed.getByTestId('direct-authorization-success').count()&&!await failed.getByTestId('direct-authorization-info').count(),'Failed bootstrap showed success or remembered-access guidance');
  const proof={ok:true,authorizationFeedback:{confirmed:true,pendingHidden:true,failedHidden:true,diagnosticsFailurePreservesSuccess:true,failedSessionDOM:successDOM},proof:'Enrollment-only real Authority and DirectServer; initial nonce expiry, fresh single-click enrollment, pending/failed sessions, cleared grant fragment, null opener, invalid address, blocked popup, setup/POST denial, retry, busy reentry, captured address, native disabled styles, stable parent feedback and independent diagnostics failure.',native_bridge:false};
  writeFileSync(path.join(artifacts,'enrollment-report.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
  } catch(error) {failure=error;}
  finally {
    for(const release of releaseGates)release();
    const cleanup=await Promise.allSettled(cleanupRoutes.map(remove=>remove()));
    if(!ingress.isClosed())await ingress.evaluate(()=>{if(window.fixtureOpen){window.open=window.fixtureOpen;delete window.fixtureOpen;}}).catch(error=>{failure ||= error;});
    for(const result of cleanup)if(result.status==='rejected')failure ||= result.reason;
  }
  if(failure)throw failure;
}

try {
  if(!enrollmentOnly){
  const runs=path.join(repoRoot,'dev/virtualization/.local/runs');
  const expires=Date.now()+60000;
  while(Date.now()<expires&&!bridgeOutput){
    if(linux.exitCode!==null)throw Error('Linux bridge exited: '+linuxLog);
    if(existsSync(runs))for(const entry of readdirSync(runs)){
      if(!/^[a-f0-9]{32}$/.test(entry))continue;
      const output=path.join(runs,entry,'output');const ready=path.join(output,'bridge-ready-'+nonce+'.json');
      if(!existsSync(ready))continue;
      const value=JSON.parse(readFileSync(ready,'utf8'));
      if(value.nonce!==nonce||value.source!=='genuine-linux-native-stores')throw Error('Wrong bridge source');
      bridgeOutput=realpathSync(output);
      assert(bridgeOutput.startsWith(realpathSync(runs)+path.sep),'Bridge output outside runner ownership');
    }
    if(!bridgeOutput)await new Promise(r=>setTimeout(r,100));
  }
  if(!bridgeOutput)throw Error('Linux bridge readiness timed out: '+linuxLog);
  while(!existsSync(path.join(bridgeOutput,'projection-'+nonce+'.json'))){if(Date.now()>expires||linux.exitCode!==null)throw Error('Linux projection unavailable: '+linuxLog);await new Promise(r=>setTimeout(r,50));}
  }
  async function startFixture(extra={}) {
    child=spawn('python3',[path.join(here,'direct_fixture.py'),root],{stdio:['ignore','pipe','pipe'],env:{...process.env,...extra,PYTHONDONTWRITEBYTECODE:'1',HA_OPS_LINUX_BRIDGE_OUTPUT:bridgeOutput || '',HA_OPS_LINUX_BRIDGE_NONCE:nonce,HA_OPS_FIXTURE_ENROLLMENT_ONLY:enrollmentOnly?'1':'0'}});
    child.stderr.on('data',b=>stderr+=b);
    return await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('Fixture startup failed: '+stderr)),10000);child.stdout.on('data',b=>{output+=b;const line=output.split('\n')[0];try{const v=JSON.parse(line);clearTimeout(timer);resolve(v);}catch{}});child.once('exit',()=>reject(Error('Fixture exited: '+stderr)));});
  }
  const fixture=await startFixture();
  if(bridgeSelfTest){
    const setup=await (await fetch(fixture.ingress+'api/direct-access/setup')).json();
    const enrollment=await (await fetch(fixture.ingress+'api/direct-access/allow',{method:'POST',headers:{Origin:new URL(fixture.ingress).origin,'Content-Type':'application/json'},body:JSON.stringify({nonce:setup.nonce,address:new URL(fixture.direct).origin})})).json();
    assert(enrollment.ok,'HTTP bridge enrollment failed');
    const grant=new URLSearchParams(new URL(enrollment.url).hash.slice(1)).get('grant');
    const exchange=await fetch(new URL(fixture.direct).origin+'/direct/bootstrap',{method:'POST',headers:{Origin:new URL(fixture.direct).origin,'Content-Type':'application/json'},body:JSON.stringify({grant})});
    assert(exchange.ok,'HTTP bridge token exchange failed');const cookie=exchange.headers.get('set-cookie');
    const status=async()=>await (await fetch(new URL(fixture.direct).origin+'/direct/api/status',{headers:{Cookie:cookie}})).json();
    assert((await status()).recovery.phase==='ready','Real Linux recovery not initially ready');
    await fetch(fixture.ingress+'fixture/worker',{method:'POST'});await fetch(fixture.ingress+'fixture/outage',{method:'POST'});
    let snapshot;const until=Date.now()+15000;
    do{snapshot=await status();if(snapshot.fixture_linux.worker_exit===93&&snapshot.services?.entries?.some(e=>e.health==='unhealthy'))break;await new Promise(r=>setTimeout(r,100));}while(Date.now()<until);
    assert(snapshot.recovery.phase==='forward'&&snapshot.recovery.pending_intent&&snapshot.fixture_linux.effect_count===1,'Interrupted real Linux recovery missing');
    const before=JSON.stringify(snapshot.fixture_linux);
    child.kill('SIGTERM');await new Promise(r=>child.once('exit',r));
    await startFixture({HA_OPS_FIXTURE_DIRECT_PORT:new URL(fixture.direct).port,HA_OPS_FIXTURE_TLS_PORT:new URL(fixture.https).port,HA_OPS_FIXTURE_INGRESS_OUTAGE:'1'});
    for(let i=0;i<3;i++)assert(JSON.stringify((await status()).fixture_linux)===before,'Restart/read replayed Linux effects');
    const proof={ok:true,transport:'HTTP self-test; no browser launched',recovery:snapshot.recovery,fixture_linux:snapshot.fixture_linux};
    writeFileSync(path.join(artifacts,'bridge-self-test.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
  }else{
  browser=await chromium.connectOverCDP(process.env.HA_OPS_BROWSER_CDP_URL,{timeout:10000});context=browser.contexts()[0];
  async function record(page){
    // Refused enrollment may close its owned blank popup before CDP inspection.
    owned.push(page);
    if(page.isClosed())return page;
    try {
      const session=await context.newCDPSession(page);
      const {targetInfo}=await session.send('Target.getTargetInfo');
      console.log(JSON.stringify({ownedTab:targetInfo.targetId}));
      await session.detach();
    } catch(error) {
      if(!/No session with given id|Session closed|Target closed|No target with given id|Target page, context or browser has been closed/.test(error.message))throw error;
    }
    return page;
  }
  if(enrollmentOnly){
    await enrollmentChecks(fixture,context,record);
  }else{
  const ingress=await record(await context.newPage());await ingress.goto(fixture.ingress);
  await ingress.getByRole('textbox',{name:'Direct address'}).fill(new URL(fixture.direct).origin);
  await ingress.getByRole('button',{name:'Set up later'}).click();assert(await ingress.getByText('You can authorize this browser later.',{exact:false}).isVisible(),'Later setup missing');
  const opened=context.waitForEvent('page');await ingress.getByRole('button',{name:'Allow this browser',exact:true}).click();const page=await record(await opened);
  await page.getByTestId('native-diagnostics').waitFor();assert(page.url()===fixture.direct,'Fragment not cleared');
  assert(await page.evaluate(()=>document.cookie)==='','HttpOnly synthetic fixture cookie was exposed');
  // Synthetic fixture cookies only: assert attributes without logging names/values.
  const cookies=await context.cookies(fixture.direct);const cookie=cookies.find(x=>x.name.startsWith('ha_ops_'));
  assert(cookie?.httpOnly&&cookie.sameSite==='Strict'&&!cookie.secure&&cookie.expires>Date.now()/1000+300*86400,'Persistent HTTP cookie attributes');
  await ingress.request.post(fixture.ingress+'fixture/https');
  await ingress.reload();await ingress.getByRole('textbox',{name:'Direct address'}).fill(new URL(fixture.https).origin);
  const tlsOpened=context.waitForEvent('page');await ingress.getByRole('button',{name:'Allow this browser',exact:true}).click();const tlsPage=await record(await tlsOpened);
  tlsSession=await context.newCDPSession(tlsPage);await tlsSession.send('Security.setIgnoreCertificateErrors',{ignore:true});await tlsPage.reload();
  await tlsPage.getByTestId('native-diagnostics').waitFor();
  await tlsSession.send('Security.setIgnoreCertificateErrors',{ignore:false});await tlsSession.detach();
  const tlsCookies=await context.cookies(fixture.https);assert(tlsCookies.some(x=>x.name.startsWith('ha_ops_')&&x.secure&&x.httpOnly&&x.sameSite==='Strict'),'HTTPS Secure cookie absent');
  await ingress.request.post(fixture.ingress+'fixture/http');
  await page.reload();await page.getByTestId('native-diagnostics').waitFor();
  const second=await record(await context.newPage());await second.goto(fixture.direct);await second.getByTestId('native-diagnostics').waitFor();
  await ingress.request.post(fixture.ingress+'fixture/restart');await page.reload();await page.getByTestId('native-diagnostics').waitFor();
  await ingress.request.post(fixture.ingress+'fixture/worker');
  await page.waitForFunction(()=>document.querySelector('ha-ops-direct')?.snapshot?.services?.phase, null, {timeout:10000});
  await ingress.request.post(fixture.ingress+'fixture/outage');await page.reload();await page.getByTestId('native-diagnostics').waitFor();
  await page.waitForFunction(()=>{const s=document.querySelector('ha-ops-direct')?.snapshot?.services;return s?.entries?.some(e=>e.health==='unhealthy');},null,{timeout:15000});
  await page.waitForFunction(()=>{const s=document.querySelector('ha-ops-direct')?.snapshot;return s?.recovery?.phase==='forward'&&s.recovery.pending_intent&&s.fixture_linux?.worker_exit===93;},null,{timeout:15000});
  const before=await page.locator('ha-ops-direct').evaluate(app=>app.snapshot.fixture_linux);
  assert(before.effect_count===1,'Genuine Linux recovery effect count missing');
  // Replace the complete owned host process while ingress stays unavailable.
  child.kill('SIGTERM');
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture restart stop timed out')),5000);child.once('exit',()=>{clearTimeout(timer);resolve();});});
  const restarted=await startFixture({HA_OPS_FIXTURE_DIRECT_PORT:new URL(fixture.direct).port,HA_OPS_FIXTURE_TLS_PORT:new URL(fixture.https).port,HA_OPS_FIXTURE_INGRESS_OUTAGE:'1'});
  assert(restarted.direct===fixture.direct,'Restart changed direct origin');

  for(let attempt=0;attempt<3;attempt++){
    await page.reload();await page.getByTestId('native-diagnostics').waitFor();
    const after=await page.locator('ha-ops-direct').evaluate(app=>app.snapshot.fixture_linux);
    assert(JSON.stringify(after)===JSON.stringify(before),'Browser replayed genuine Linux worker');
  }
  await page.screenshot({path:path.join(artifacts,'direct-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(artifacts,'direct-mobile.png'),fullPage:true});
  assert(await page.getByRole('button',{name:'Native Apply unavailable'}).isDisabled(),'Native actions enabled');
  const proof={ok:true,proof:'Genuine Linux RecoveryStore and ServiceStore independent worker, actual durable effect-beforeACK exit93, pending recovery journal, unhealthy Core restart diagnostic, ingress outage, persistent HTTP and HTTPS browser access; repeated reads preserve actual effect/command counts.'};writeFileSync(path.join(artifacts,'report.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
  }
  }
} finally {
  if(tlsSession)await tlsSession.send('Security.setIgnoreCertificateErrors',{ignore:false}).catch(()=>{});
  console.log('Closing recorded task-owned fixture tabs after checks; existing user tabs and sessions remain.');
  for(const page of owned)if(!page.isClosed())await page.close();
  // Installed Playwright chromium connectOverCDP implementation owns only the
  // WebSocket transport: browserProcess.close=doClose=>closeAndWait=>transport.close.
  // Unlike launchPersistentContext, disconnecting this browser preserves pages.
  if(browser)await browser.close();
  if(child){child.kill('SIGTERM');await new Promise(r=>{if(child.exitCode!==null)r();else{const timer=setTimeout(()=>{child.kill('SIGKILL');r();},5000);child.once('exit',()=>{clearTimeout(timer);r();});}});}
  if(bridgeOutput&&existsSync(bridgeOutput))writeFileSync(path.join(bridgeOutput,'stop-'+nonce),'stop');
  const code=linux ? await new Promise(r=>{if(linux.exitCode!==null)r(linux.exitCode);else{const timer=setTimeout(()=>{linux.kill('SIGINT');r(null);},20000);linux.once('exit',code=>{clearTimeout(timer);r(code);});}}) : 0;
  rmSync(root,{recursive:true,force:true});
  if(code!==0)throw Error('Linux bridge cleanup did not complete: '+linuxLog);
  assert(!bridgeOutput||!existsSync(bridgeOutput),'Owned Linux bridge output was retained after cleanup');
}
