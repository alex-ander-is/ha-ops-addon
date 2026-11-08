/* Deferred-response regressions against the actual component methods. */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const file=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../frontend/src/migration-review.js');
const source=fs.readFileSync(file,'utf8').replace(/^import .*;$/gm,'').replace('export class','class')+'\nglobalThis.Review=HaOpsMigrationReview;';
const listeners=new Map();
const sandbox={LitElement:class{connectedCallback(){}disconnectedCallback(){}},html:()=>'',css:()=>'',nothing:null,customElements:{define(){}},window:{addEventListener(name,fn){listeners.set(name,fn);},removeEventListener(name,fn){if(listeners.get(name)===fn)listeners.delete(name);}},TextDecoder,Uint8Array,fetch:async()=>({}),setInterval:()=>1,clearInterval:()=>{}};
vm.runInNewContext(source,sandbox);
const model={candidate:'candidate',scope_digest:'scope',complete:true};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const fresh=()=>{const c=new sandbox.Review();c.model=model;c.state={csrf:'fixture',status:'current'};c.ready=true;c.reviewId='current';c.allowedActions=['approve','refuse','dismiss'];c.contentAvailable=true;return c;};
let cases=0;
for(const [name,start,waiting,result] of [
 ['review',c=>c.review(),'review',{model,status:'current',review_id:'late'}],
 ['open-first',c=>c.open(0,'after'),'content-open',{view_id:'late'}],
 ['open-page',c=>c.open(0,'after'),'content-page',{data:'YQ==',offset:0,total:1}],
 ['move',c=>c.move(1),'content-page',{data:'Yg==',offset:1,total:2}],
 ['decision',c=>c.decide('dismiss'),'decision',{decision:{action:'dismiss'}}],
]){
 for(const lifecycle of ['offline','disconnect','new-load']){
  const c=fresh();c.content={view_id:'initial'};c.page={data:'old'};const pending=deferred();let calls=[];
  c.api=async route=>{calls.push(route);if(route===waiting)return pending.promise;if(route==='state')return {model,status:'current'};if(route==='content-open')return {view_id:'opened'};throw Error('unexpected '+route);};
  const operation=start(c);await tick();
  if(lifecycle==='disconnect')c.disconnectedCallback();else if(lifecycle==='new-load')await c.load();else c.invalidate('Connection changed');
  pending.resolve(result);await operation;
  assert.equal(c.reviewId,null,name+'/'+lifecycle+' revived review');
  assert.equal(c.content,null,name+'/'+lifecycle+' revived selection');
  assert.equal(c.page,null,name+'/'+lifecycle+' revived bytes');
  assert.equal(c.hiddenPane,false,name+'/'+lifecycle+' revived dismissal');
  if(name==='open-first')assert.equal(calls.filter(x=>x==='content-page').length,0,'stale open launched second request');
  cases++;
 }
}
{
 const c=fresh(),first=deferred(),second=deferred();let n=0;c.api=()=>++n===1?first.promise:second.promise;
 const old=c.review();c.invalidate('offline');c.ready=true;const current=c.review();first.resolve({model,status:'current',review_id:'stale'});await old;
 assert.equal(c.busy,true,'old finally cleared newer action busy');assert.equal(c.reviewId,null);
 second.resolve({model,status:'current',review_id:'fresh'});await current;assert.equal(c.reviewId,'fresh');assert.equal(c.busy,false);cases++;
}
{
 const c=fresh(),first=deferred(),second=deferred();let n=0;c.api=()=>++n===1?first.promise:second.promise;
 c.hiddenPane=true;const old=c.load();const current=c.load();assert.equal(c.hiddenPane,false);
 first.resolve({model:{candidate:'old'}});await old;assert.equal(c.model,null);assert.equal(c.busy,true);
 second.resolve({model,status:'current'});await current;assert.equal(c.model.candidate,'candidate');assert.equal(c.hiddenPane,false);assert.equal(c.reviewId,null);cases++;
}
{
 const c=fresh(),pending=deferred();c.content={view_id:'old'};c.api=()=>pending.promise;const old=c.closeContent();
 c.invalidate('offline');c.content={view_id:'new'};c.page={data:'new'};pending.resolve({});await old;
 assert.equal(c.content.view_id,'new');assert.equal(c.page.data,'new');cases++;
}
{
 const c=fresh();c.api=async()=>({model,status:'current'});c.connectedCallback();await tick();c.hiddenPane=true;
 const generation=c.generation;listeners.get('offline')();assert(c.generation>generation);assert.equal(c.reviewId,null);
 await listeners.get('online')();assert.equal(c.hiddenPane,false);assert.equal(c.reviewId,null);
 c.hiddenPane=true;await listeners.get('ha-ops-migration-refresh')();assert.equal(c.hiddenPane,false);
 listeners.get('ha-ops-migration-reset')();assert.equal(c.reviewId,null);c.disconnectedCallback();assert.equal(listeners.size,0);cases++;
}

for(const status of ['stale','unavailable','absent','pending','unknown']){
 const c=fresh();c.api=async()=>({status,model});await c.load();let calls=0;c.api=async()=>{calls++;return {model,status:'current',review_id:'unexpected'};};
 assert.equal(c.canReview(),false,status+' model enabled Review');await c.review();assert.equal(calls,0);cases++;
}
for(const status of ['unchecked','current']){
 const c=fresh();c.api=async()=>({status,model});await c.load();assert.equal(c.canReview(),true,status+' fresh model blocked first explicit Review');
 assert.equal(c.reviewId,null);let calls=0;c.api=async()=>{calls++;return {status:'current',model,review_id:'explicit'};};
 await c.review();assert.equal(calls,1);assert.equal(c.reviewId,'explicit');cases++;
}
for(const reset of ['invalidate','failed-load','failed-action']){
 const c=fresh();let calls=0;
 if(reset==='invalidate')c.invalidate('offline');
 else if(reset==='failed-load'){c.api=async()=>{throw Error('failed state');};await c.load();}
 else await c.act(async()=>{throw Error('failed action');});
 c.api=async()=>{calls++;return {model,status:'current',review_id:'unexpected'};};
 assert.equal(c.canReview(),false,reset+' retained readiness');await c.review();assert.equal(calls,0);
 c.api=async()=>({status:'current',model});await c.load();assert.equal(c.canReview(),true);assert.equal(c.reviewId,null);cases++;
}
for(const action of ['approve','refuse','dismiss']){
 const c=fresh(),calls=[],released=[],cancelled=[];let queued;
 sandbox.setInterval=callback=>{queued=callback;return 42;};sandbox.clearInterval=id=>cancelled.push(id);
 c.content={view_id:'terminal-'+action};c.page={data:'old'};
 c.release=content=>{if(content)released.push(content.view_id);};
 c.api=async(route,value)=>{calls.push([route,value]);return {decision:{action}};};
 c.startHeartbeat();await c.decide(action);await queued();
 assert.equal(c.heartbeat,null);assert.equal(c.content,null);assert.equal(c.page,null);
 assert.equal(c.hiddenPane,action==='dismiss');assert.deepEqual(cancelled,[42]);
 assert.deepEqual(released,['terminal-'+action]);assert.equal(calls.length,1);assert.equal(calls[0][0],'decision');
 cases++;
}
console.log(JSON.stringify({status:'passed',cases,source:'actual migration-review.js deferred lifecycle methods'}));
