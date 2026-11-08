import assert from 'node:assert/strict';
import {intercept} from './migration-review-interception.mjs';
for(const label of ['state','delayed']){
 let callback,timeout,aborted=0,cleaned=false,removed=false;
 const page={async route(pattern,fn){callback=fn;},async unroute(){removed=true;}};
 const route={async fetch(options){timeout=options.timeout;throw Error(label+' fetch failed');},async abort(){aborted++;}};
 const item=await intercept(page,label,()=>{throw Error('failed fetch reached handler');});
 try{
  await item.guard(async()=>{await callback(route);await new Promise(()=>{});});
  assert.fail('callback failure not propagated');
 }catch(error){assert.equal(error.message,label+' fetch failed');}
 finally{await item.close();cleaned=true;}
 assert.equal(timeout,120000);assert(cleaned&&removed&&aborted>0);
}
// A successful fetch held beyond the old default is handled without real waiting.
let callback,seen=false,aborted=0;
const page={async route(p,fn){callback=fn;},async unroute(){}};
const route={async fetch({timeout}){assert.equal(timeout,120000);return {ok:true};},async abort(){aborted++;}};
const item=await intercept(page,'success',async(r,response)=>{seen=response.ok;});
await item.guard(()=>callback(route));assert(seen);await item.close();assert(aborted);
console.log('interception failures propagate through outer cleanup');
