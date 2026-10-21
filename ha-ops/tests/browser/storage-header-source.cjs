/* Focused logic guards; shipped Playwright DOM evidence remains required. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const source=fs.readFileSync(path.resolve(__dirname,'../../frontend/src/ha-ops.js'),'utf8');
const block=source.slice(source.indexOf('class HaOpsStorage '),source.indexOf('class HaOpsApp '));let Storage;
vm.runInNewContext(block,{LitElement:class{},css:()=>'',html:(strings,...values)=>({strings,values}),nothing:Symbol(),crypto:crypto.webcrypto,customElements:{define:(name,type)=>{Storage=type;}}});
const text=value=>Array.isArray(value)?value.map(text).join(''):value?.strings?value.strings.map((part,index)=>part+text(value.values[index])).join(''):typeof value==='symbol'||typeof value==='function'||value==null?'':String(value);
const categories=['git','staging','diff','recovery','service','authorization'].map((category,index)=>({category,allocated_bytes:index+1,logical_bytes:10000+index}));
const measure={completed_at:100,categories,volumes:[{warning:false,available_bytes:999999999,allocated_bytes:8888888}],pending:{status:'none'}};
const render=(measurement,extra={})=>{const control=new Storage();control.model={qualified:true,measurement,attempt:{status:'completed'},...extra};return text(control.render()).split('<vaadin-details')[0];};
const scenario=process.argv[2];let checks=0;
if(scenario==='overhead'){
  assert(render(null).includes('HA Ops overhead: Unknown'),'missing measurement must visibly be Unknown');checks++;
  assert(render(measure).includes('HA Ops overhead: 21 bytes physical allocated'),'header must sum six physical categories including authorization');checks++;
  assert(render({...measure,categories:categories.map(row=>({...row,allocated_bytes:0}))}).includes('HA Ops overhead: 0 bytes physical allocated'),'zero differs from Unknown');checks++;
}else if(scenario==='warning'){
  for(const volumes of [[{warning:false},{warning:true}],[{warning:true},{warning:false}]]){
    assert(render({...measure,volumes},{maintenance:{status:'completed'}}).includes('severity=warning .label=WARNING'),'low any-volume must override completed maintenance');checks++;
  }
  assert(render({...measure,volumes:[{warning:false,available_bytes:0}]}).includes('severity=idle'),'backend false flag is authoritative');checks++;
  for(const status of ['failed','unavailable']){assert(render(measure,{attempt:{status,error:'failure'},maintenance:{status:'completed'}}).includes('severity=error'),'attempt error precedence');checks++;}
  for(const status of ['effect-started','uncertain']){assert(render(measure,{maintenance:{status}}).includes('severity=warning'),'maintenance warning retained');checks++;}
  for(const status of ['incomplete','uncertain']){assert(render(measure,{attempt:{status,error:'incomplete'}}).includes('severity=warning'),'attempt warning retained');checks++;}
}else throw Error('unknown scenario');
console.log(JSON.stringify({scenario,checks,status:'passed',rendered:false}));
