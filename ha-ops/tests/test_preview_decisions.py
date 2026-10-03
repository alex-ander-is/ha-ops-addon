"""Portable frontend behavior guards; rendered proof is a separate browser flow.

The Node helper evaluates the complete production module with inert Lit/Vaadin
imports. It does not claim DOM retention, native focus or visual verification.
HA_OPS_FRONTEND_SOURCE permits a disposable historical source for red/green proof.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


HELPER = Path(__file__).parent / "browser" / "preview-decisions-source.cjs"


@pytest.mark.parametrize("scenario", [
    "local", "lifetime", "final", "backup", "fences", "native", "lazyDiff", "transport",
])
def test_complete_frontend_preview_decision_contract(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node frontend runtime required for complete-module behavior regressions")
    result = subprocess.run(
        [node, str(HELPER), scenario], check=False, capture_output=True, text=True,
        timeout=30, env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["completeProductionModule"] is True
    assert evidence["rendered"] is False
    assert evidence["checks"] > 0


@pytest.mark.parametrize("stage", [
    "newPage", "newCDPSession", "targetInfo", "page_close", "connect",
    "harness_error", "malformed_ready", "second_cdp", "multi_close",
    "runtime_write", "runtime_write_owned", "detach", "kill", "signal_exit", "spawn",
    "launch", "partial_ready", "cleanup_write", "root_remove",
    "newPage_pending", "session_pending", "target_pending", "connect_pending", "launch_pending",
    "newPage_late", "session_late", "target_late", "connect_late", "launch_late",
    "close_pending", "detach_pending", "second_close_pending",
    "fixture_pending", "fixture_json_pending", "viewport_pending",
    "newPage_after_grace", "session_after_grace",
])
def test_backup_browser_owned_resource_cleanup(stage):
    """Execute actual runner acquisition/teardown with inert process/browser/fs."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for browser runner cleanup regression")
    runner = os.environ.get("HA_OPS_BACKUP_RUNNER_SOURCE", str(HELPER.with_name("backup-continuation.mjs")))
    script = r'''
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const source=fs.readFileSync(process.argv[1],'utf8'),stage=process.argv[2];
let body=source.slice(source.indexOf('const artifacts ='));
if(['second_cdp','multi_close','second_close_pending'].includes(stage)) {
  assert(body.includes('await page.goto(baseUrl);'));
  body=body.replace('await page.goto(baseUrl);', "await acquirePage(); throw new Error('flow failure');");
}
if(['fixture_pending','fixture_json_pending'].includes(stage))body=body.replace('await page.goto(baseUrl);',"await fixtureJson('http://fixture/test'); throw new Error('flow failure');");
if(stage==='viewport_pending')body=body.replace('await page.goto(baseUrl);',"await bounded(page.setViewportSize({}), 'viewport update'); throw new Error('flow failure');");
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
(async()=>{
  const writes=[],removed=[],created=[],closed=[],kills=[];
  let detached=0,userClosed=0,exited=false;
  const delayedTimers=[];
  const delayed=(value)=>stage.endsWith('_pending')?new Promise(()=>{}):new Promise(resolve=>{
    delayedTimers.push(setTimeout(()=>resolve(value),stage.endsWith('_after_grace')?180:35));
  });
  const accelerated=(callback,milliseconds)=>setTimeout(callback,milliseconds===1000?60:20);
  const userPage={url:()=> 'http://user-owned/',close:async()=>{userClosed++;throw Error('user tab touched')}};
  const ctx={pages:()=>[userPage,...created.filter(p=>!p.isClosed())],newPage:async()=>{
    if(stage==='newPage')throw Error(stage);
    if(stage==='newPage_pending')return delayed();
    const index=created.length;let didClose=false;
    const p={url:()=> 'about:blank',isClosed:()=>didClose,close:async()=>{
      closed.push(index);if(stage==='close_pending'||stage==='second_close_pending'&&index===1)return new Promise(()=>{});if(['page_close','multi_close'].includes(stage)&&index===0)throw Error('close failed');didClose=true;
    },setViewportSize:async()=>new Promise(()=>{}),goto:async()=>{throw Error('flow failure')}};
    created.push(p);return ['newPage_late','newPage_after_grace'].includes(stage)?delayed(p):p;
  },newCDPSession:async p=>{
    if(stage==='newCDPSession'||stage==='second_cdp'&&created.indexOf(p)===1)throw Error(stage);
    const session={send:async()=>{if(stage==='targetInfo')throw Error(stage);const info={targetInfo:{targetId:`owned-${created.indexOf(p)}`}};return ['target_pending','target_late'].includes(stage)?delayed(info):info},
      detach:async()=>{detached++;if(stage==='detach')throw Error(stage);if(stage==='detach_pending')return new Promise(()=>{})}};
    return ['session_pending','session_late','session_after_grace'].includes(stage)?delayed(session):session;
  }};
  const child=new EventEmitter();child.pid=123;child.exitCode=null;child.signalCode=null;child.stdout=new EventEmitter();
  child.kill=signal=>{kills.push(signal);if(stage==='kill'&&signal==='SIGINT')throw Error('kill failed');
    child.signalCode=signal;setImmediate(()=>child.emit('exit',null,signal));return true};
  const mockSpawn=()=>{
    if(stage==='spawn')throw Error(stage);
    setImmediate(()=>{
      if(stage==='harness_error')child.emit('error',new Error(stage));
      else if(stage==='malformed_ready')child.stdout.emit('data','{broken}\n');
      else {
        if(stage==='signal_exit')child.signalCode='SIGTERM';
        const ready='{"baseUrl":"http://fixture/","root":"owned-fixture"}\n';
        if(stage==='partial_ready') {child.stdout.emit('data',ready.slice(0,12));setImmediate(()=>child.stdout.emit('data',ready.slice(12)));}
        else child.stdout.emit('data',ready);
      }
    });return child;
  };
  const fakeProcess={env:stage.startsWith('launch')?{}:{HA_OPS_BROWSER_CDP_URL:'mock'},pid:456,exitCode:0,exit:()=>{exited=true;delayedTimers.forEach(clearTimeout)}};
  let uncaught=null;
  try {await Promise.race([new AsyncFunction('spawn','mkdirSync','writeFileSync','mkdtempSync','rmSync','path','os','process','chromium','here','sharedRoot','console','setTimeout','clearTimeout','fetch',body)(
    mockSpawn,()=>{},(p,d)=>{const data=JSON.parse(d);if((stage==='runtime_write'||stage==='runtime_write_owned'&&data.ownedPages?.length)&&p.endsWith('/runtime.json')||stage==='cleanup_write'&&p.endsWith('/cleanup.json'))throw Error(stage);writes.push({path:p,data})},
    ()=> 'owned-fixture',(p)=>{removed.push(p);if(stage==='root_remove')throw Error(stage)},path,os,fakeProcess,
    {connectOverCDP:async()=>{if(stage==='connect')throw Error(stage);const browser={contexts:()=>[ctx]};return ['connect_pending','connect_late'].includes(stage)?delayed(browser):browser},
      launchPersistentContext:async()=>{if(stage==='launch')throw Error('launch failed');return delayed(ctx)}},'/fixture','/shared',{log:()=>{},error:()=>{}},accelerated,clearTimeout,async()=>stage==='fixture_pending'?new Promise(()=>{}):{json:async()=>new Promise(()=>{})}),new Promise((_,reject)=>setTimeout(()=>reject(Error('runner never reached cleanup')),250))]);
  }catch(error){uncaught=error.message}
  assert(exited,'runner did not reach bounded exit');
  assert.equal(uncaught,null,'runner must report failure after best-effort cleanup');
  assert.equal(fakeProcess.exitCode,1,'injected failure must fail verification');
  assert.deepEqual(removed,['owned-fixture'],'owned fixture must be removed after harness exit');
  if(stage!=='spawn'&&stage!=='signal_exit')assert(kills.length>0,'owned harness not stopped');
  if(stage==='signal_exit')assert.equal(kills.length,0,'already signaled child must not be killed again');
  if(stage==='kill')assert(kills.includes('SIGTERM'),'failed SIGINT must not prevent fallback stop');
  const zeroPages=['newPage','connect','harness_error','malformed_ready','runtime_write','spawn','launch','newPage_pending','connect_pending','connect_late','launch_pending','launch_late'];
  assert.equal(created.length,zeroPages.includes(stage)?0:['second_cdp','multi_close','second_close_pending'].includes(stage)?2:1,'unexpected acquisition stage');
  assert(created.every((p,i)=>stage==='newPage_after_grace'||closed.includes(i)),'every acquired owned page must get independent cleanup');
  if(['targetInfo','detach','target_pending','target_late','session_late'].includes(stage))assert(detached>0,'acquired CDP session must be detached');
  const cleanup=writes.find(w=>w.path.endsWith('/cleanup.json'))?.data;
  assert.equal(userClosed,0,'existing user page must never be closed');
  if(stage!=='cleanup_write'){assert(cleanup,'cleanup evidence missing');assert.equal(cleanup.harnessExited,true);}
  if(['newPage_pending','session_pending','connect_pending','launch_pending','newPage_after_grace','session_after_grace'].includes(stage))assert(cleanup.pendingOperations?.length>0 && cleanup.cleanupIncomplete,'uncertain late ownership must be explicit');
  if(stage.endsWith('_late'))assert.equal(cleanup.pendingOperations.length,0,'late completion must settle within grace');
  if(stage==='newPage_late')assert(created[0].isClosed(),'late owned page leaked');
  if(stage==='session_late')assert(detached>0,'late acquired session leaked');
  if(stage==='root_remove')assert.equal(cleanup.harnessRootRemoved,false);
  if(['page_close','multi_close','detach','kill','root_remove'].includes(stage))assert(cleanup.errors.length>0,'cleanup failures must be reported');
  console.log(JSON.stringify({stage,result:'passed',created:created.length,closed,kills,fixtureRemoved:stage!=='root_remove',userPagePreserved:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script, runner, stage], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"] == "passed"
