/* Execute the complete frontend with inert Lit imports. These are logic contracts,
 * not DOM, Vaadin focus, paint, or node-identity evidence; see preview-decisions.mjs. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const sourcePath = process.env.HA_OPS_FRONTEND_SOURCE || path.resolve(__dirname, '../../frontend/src/ha-ops.js');
const registered = new Map();
const timers = new Map();
let timerId = 0;
const context = {
  LitElement: class { requestUpdate() {} disconnectedCallback() {} },
  css: () => '', html: (strings, ...values) => ({ strings, values }), nothing: Symbol('nothing'),
  customElements: { define: (name, klass) => registered.set(name, klass) },
  document: { activeElement: null, body: {}, removeEventListener() {} },
  window: { WebSocket: { OPEN: 1, CLOSED: 3 }, location: { href: 'http://localhost/' } },
  location: { href: 'http://localhost/' }, console, crypto: crypto.webcrypto,
  TextEncoder, URL, structuredClone, CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
  requestAnimationFrame: callback => callback(),
  setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
  clearTimeout: id => timers.delete(id),
};
const source = fs.readFileSync(sourcePath, 'utf8').replace(/^import .*;\n/gm, '').replace(/^export (?=function )/gm, '');
vm.runInNewContext(source, context, { filename: sourcePath });
const App = registered.get('ha-ops-app');
const Preview = registered.get('ha-ops-preview');
const File = registered.get('ha-ops-preview-file');
function fixture() {
  const a = new App(); a.connection = 'http'; a.replayPending = false;
  a.state = { operation_generation: 7, last_status: 'success', command_records: {},
    apply_preview_id: 'apply-content', save_preview_id: 'save-content',
    apply_decision_revision: 3, save_decision_revision: 5,
    last_preview_paths: ['a.yaml', 'b.yaml'], last_save_preview_paths: ['a.yaml', 'b.yaml'],
    last_preview_commit: 'commit-a', last_save_preview_commit: 'commit-s',
    last_preview_fingerprint: 'fingerprint-a', last_save_preview_fingerprint: 'fingerprint-s',
    last_diff_cursor: { schema: 1, kind: 'apply', generation: 7, artifact: 'a', sha256: 'aa', bytes: 10 },
    last_save_diff_cursor: { schema: 1, kind: 'save', generation: 7, artifact: 's', sha256: 'ss', bytes: 12 },
    apply_preview_selected_paths: ['a.yaml'], save_preview_selected_paths: ['a.yaml'],
    apply_preview_resolutions: {}, save_preview_resolutions: {}, last_save_commit_subject: 'default subject' };
  a.view = { display_times: {} }; return a;
}
let checks = 0;
const event = detail => ({ detail, stopPropagation() {} });
function preview(direction = 'apply') {
  const p = new Preview(); p.direction = direction; p.state = fixture().state;
  p.events = []; p.dispatchEvent = e => { p.events.push(e.detail); }; p.willUpdate(); return p;
}
async function local() {
  for (const direction of ['apply', 'save']) {
    const p = preview(direction), a = fixture();
    const initialSelection = p.selectedPaths.length;
    context.fetch = () => { throw new Error('local edit attempted fetch'); };
    p.onPreviewSelect(event({path:'a.yaml', selected:true}));
    assert.equal(p.events.length,0,'local edit attempted command dispatch');
    assert.equal(initialSelection,0,'must never hydrate server decisions');
    p.onPreviewSelect(event({path:'b.yaml', selected:true}));
    p.onPreviewSelect(event({path:'a.yaml', selected:false}));
    assert.deepEqual([...p.selectedPaths], ['b.yaml']);
    p.selectAll(true); p.onPreviewResolve(event({path:'a.yaml',choice:'ha'}));
    p.onPreviewResolve(event({path:'a.yaml',choice:'git'}));
    assert.equal(p.resolutions['a.yaml'],'git'); p.selectAll(false);
    assert.equal(p.selectedPaths.length,0); assert.equal(p.events.length,0,'local edit attempted command dispatch');
    for (const connection of ['unknown','reconnecting','http']) {
      a.connection=connection; a.replayPending=true; assert(!a.previewEditBlocked());
      p.selectAll(true); assert.equal(p.selectedPaths.length,2); assert.equal(p.events.length,0);
    }
    assert.equal(a.commandIntent,null); assert.equal(a.acceptedCommandId,null); checks++;
  }
}
async function lifetime() {
  for (const direction of ['apply','save']) {
    const p=preview(direction); p.selectAll(true); p.resolutions={'a.yaml':'ha'}; p.wrapByPath={'a.yaml':false}; p.commitSubject='edited';
    p.state={...p.state,[`${direction}_decision_revision`]:99,[`${direction}_preview_selected_paths`]:[]}; p.willUpdate();
    assert.equal(p.selectedPaths.length,2); assert.equal(p.resolutions['a.yaml'],'ha'); assert.equal(p.wrapByPath['a.yaml'],false);
    if(direction==='save') assert.equal(p.commitSubject,'edited');
    for(const field of ['operation_generation',direction==='save'?'last_save_preview_fingerprint':'last_preview_fingerprint']) {
      p.state={...p.state,[field]:'replacement'}; p.willUpdate(); assert.equal(p.selectedPaths.length,0); assert.equal(Object.keys(p.resolutions).length,0); p.selectAll(true);
    }
    p.backendVersion='3.0.0'; p.willUpdate(); assert.equal(p.selectedPaths.length,0);
    const other=preview(direction); assert.equal(other.selectedPaths.length,0); checks++;
  }
}
async function final() {
  for(const direction of ['apply','save']) {
    const p=preview(direction); p.selectAll(true); p.resolutions={'a.yaml':'ha'}; p.commitSubject='reviewed';
    p.runFinalAction(); assert.equal(p.events.length,1,'final dispatch must be synchronous');
    const e=p.events[0]; assert.equal(e.command,direction); assert.equal(e.payload.selected_paths.length,2); assert(!('decision_digest' in e.payload));
    p.selectedPaths=[]; p.resolutions['a.yaml']='git'; p.state.last_preview_paths.push('later.yaml'); p.commitSubject='later';
    assert.equal(e.payload.selected_paths.length,2); assert.equal(e.payload.resolutions['a.yaml'],'ha'); assert.equal(e.payload.preview_identity.paths.length,2);
    if(direction==='save') assert.equal(e.payload.commit_subject,'reviewed');
    p.runFinalAction(); assert.equal(p.events.length,1); checks++;
  }
}
async function backup() {
  const p=preview(); assert(!p.backupRefusal); assert(!p.backupWarning);
  const refusal = id => ({operation_id:id,generation:p.state.operation_generation,max_age_hours:12,preview_identity:Object.fromEntries(Object.entries(context.structuredClone(vm.runInNewContext('previewIdentity',context)(p.state,'apply'))).sort(([a],[b])=>a.localeCompare(b)))});
  p.state.apply_backup_refusal=refusal('old');
  assert(p.backupWarning,'reload must expose non-actionable diagnostic');
  assert.equal(p.submitted,false); assert.equal(p.selectedPaths.length,0); assert(!p.backupRefusal);
  p.runFinalAction('acknowledge'); p.runFinalAction('retry'); assert.equal(p.events.length,0);
  p.selectAll(true); p.runFinalAction(); p.running=false;
  p.state.apply_backup_refusal=refusal('submitted'); assert(p.backupRefusal);
  p.wrapAll(false); assert(p.backupRefusal,'wrap is not a processing decision');
  p.onPreviewSelect(event({path:'a.yaml',selected:false})); assert(!p.backupRefusal); assert(p.backupWarning);
  p.onPreviewSelect(event({path:'a.yaml',selected:true})); assert(!p.backupRefusal); assert(p.backupWarning);
  p.onPreviewResolve(event({path:'a.yaml',choice:'ha'})); p.onPreviewResolve(event({path:'a.yaml',choice:'git'}));
  p.state={...p.state,last_message:'unrelated state update'}; p.willUpdate(); assert(!p.backupRefusal); assert(p.backupWarning);
  p.runFinalAction(); p.running=false; assert(!p.backupRefusal,'dismissed old refusal must never revive');
  p.state={...p.state,apply_backup_refusal:refusal('new')}; assert(p.backupRefusal);
  const current=p.state;
  for(const field of ['apply_preview_id','last_preview_commit','last_preview_fingerprint','last_preview_paths','last_preview_conflict_paths','last_preview_live_fingerprints','last_diff_cursor','apply_decision_revision','operation_generation']) {
    const changed=field.endsWith('paths')?['different']:field.endsWith('fingerprints')?{changed:'different'}:field==='last_diff_cursor'?{...current.last_diff_cursor,artifact:'different'}:field.endsWith('revision')||field==='operation_generation'?99:'different';
    p.state={...current,[field]:changed}; assert(!p.backupWarning,field); assert(!p.backupRefusal,field);
  }
  p.state={...current,apply_backup_refusal:null,last_status:'warning',last_message:'No fresh system backup found within 24 hour(s)'};
  assert(!p.backupWarning); assert(!p.backupRefusal);
  p.state={...current,apply_backup_refusal:null,last_status:'error'}; assert(!p.backupWarning);
  p.state={...current,last_preview_paths:[]}; assert(!p.backupWarning);
  checks++;
}
async function fences() {
  for(const gate of ['intent','accepted','uncertain','operation','running','recovery','prune']) {
    const a=fixture(); if(gate==='intent')a.commandIntent={}; if(gate==='accepted')a.acceptedCommandId='x'; if(gate==='uncertain')a.uncertainCommandId='x';
    if(gate==='operation')a.state.active_operation={}; if(gate==='running')a.state.last_status='running'; if(gate==='recovery')a.state.deleted_devices_recovery_phase='restore'; if(gate==='prune')a.state.docker_build_cache_prune_fence=true;
    assert(a.previewEditBlocked(),gate); assert(a.previewTemplate()!==context.nothing,'loaded same-content preview must remain mounted'); checks++;
  }
  const p=preview('save'); p.state.last_save_preview_conflict_paths=['a.yaml']; p.selectAll(true); assert(p.isFinalActionDisabled());
  p.resolutions={'a.yaml':'git'}; assert(!p.isFinalActionDisabled()); p.finalBlocked=true; assert(p.isFinalActionDisabled()); p.selectAll(false); assert.equal(p.selectedPaths.length,0); checks++;
}
async function native() {
  const f=new File(); f.path='a.yaml'; f.selected=false; let selection;
  f.dispatchEvent=e=>selection=e.detail.selected;
  const host={checked:true}; f.onSelectChange({currentTarget:host}); assert(selection); assert(host.checked,'native checked must not roll back'); checks++;
}
async function lazyDiff() {
  for (const direction of ['apply', 'save']) {
    const p = new Preview(); p.direction = direction; p.state = fixture().state; p.willUpdate();
    p.wrapByPath = { 'a.yaml': false }; p.commitSubject = 'edited subject';
    p.state = { ...p.state, [`${direction}_decision_revision`]: 99 }; p.willUpdate();
    assert.equal(p.wrapByPath['a.yaml'], false); if (direction === 'save') assert.equal(p.commitSubject, 'edited subject');
    p.state = { ...p.state, operation_generation: 8 }; p.willUpdate(); assert.equal(Object.keys(p.wrapByPath).length, 0);
    if (direction === 'save') assert.equal(p.commitSubject, 'default subject'); checks++;
  }
  for (const field of ['schema', 'kind', 'generation', 'artifact', 'sha256', 'bytes', 'path', 'contentKey']) for (const outcome of ['success', 'error']) {
    const f = new File(); f.path = 'a.yaml'; f.cursor = fixture().state.last_diff_cursor; f.generation = 7; f.contentKey = 'current'; f.isConnected = true;
    let finish, fail; context.fetch = () => new Promise((resolve, reject) => { finish = resolve; fail = reject; });
    const promise = f.setExpanded(true); const changed = new Map();
    if (field === 'path' || field === 'contentKey') { changed.set(field, f[field]); f[field] = 'replacement'; }
    else { changed.set('cursor', f.cursor); f.cursor = { ...f.cursor, [field]: `replacement-${field}` }; }
    f.willUpdate(changed);
    if (outcome === 'success') finish({ json: async () => ({ ok: true, diff: 'STALE', semantic: { stale: true } }) }); else fail(new Error('old error'));
    await promise; assert(!f.expanded); assert.equal(f.diff, ''); assert.equal(f.semantic, null); assert.equal(f.diffState, 'idle'); checks++;
  }
  const f = new File(); f.path = 'a.yaml'; f.cursor = fixture().state.last_diff_cursor; f.generation = 7; f.contentKey = 'current'; f.isConnected = true;
  let finish; let requests = 0; context.fetch = () => { requests++; return new Promise(resolve => finish = resolve); };
  const promise = f.setExpanded(true); f.selected = true; f.willUpdate(new Map([['selected', false]]));
  finish({ json: async () => ({ ok: true, diff: 'current diff', semantic: { counts: {} } }) }); await promise;
  assert.equal(f.diff, 'current diff'); assert(f.expanded); await f.setExpanded(true); assert.equal(requests, 1); checks++;
}
async function transport() {
  for(const command of ['apply','save']) for(const outcome of ['terminal','ambiguous']) {
    const a=fixture(), sent=[]; a.socket={readyState:1,send:raw=>sent.push(JSON.parse(raw))};
    const payload={selected_paths:['a.yaml'],resolutions:{}};
    const promise=a.dispatchCommand(command,'final',payload).catch(e=>e.message);
    assert(a.commandIntent,'real mutation must fence synchronously'); assert(a.previewEditBlocked());
    payload.selected_paths.push('b.yaml'); assert.equal(sent[0].payload.selected_paths.length,1);
    await a.dispatchCommand(command,'final',{}); assert.equal(sent.length,1);
    const id=sent[0].command_id;
    if(outcome==='terminal') {
      a.receive({type:'result',id:sent[0].id,ok:true}); await promise;
      a.state.command_records[id]={command,status:'terminal'}; a.reconcileAcceptedCommand(); assert(!a.mutationBlocked());
    } else {
      const entry=[...a.pending.values()][0]; entry.reject(new Error('lost response')); await promise;
      assert(a.uncertainCommandId); await a.dispatchCommand(command,'final',{}); assert.equal(sent.length,1);
    }
    checks++;
  }
}
async function badges() {
  const Badge=registered.get('ha-ops-status-badge');
  assert(Badge, 'shared status badge must register in the complete module');
  const badge=new Badge();
  context.fetch=()=>{throw Error('presentation attempted network')};
  badge.dispatchEvent=()=>{throw Error('presentation attempted command')};
  for(const [severity,theme] of [['idle','success'],['success','success'],['error','error'],['running',''],['conflicts',''],['interrupted','warning'],['pending','warning'],['pending decision','warning'],['warning','warning'],['transport','warning'],['version',''],['neutral',''],['future','success']]) {
    badge.severity=severity; badge.label='<b> & literal >';
    const rendered=badge.render();
    assert.equal(badge.badgeTheme,theme);
    assert(rendered.strings.join('').includes('<vaadin-badge'));
    assert(rendered.values.includes('<b> & literal >'),'label must stay a text binding'); checks++;
  }
  const renderText=value=>value&&value.strings?value.strings.reduce((s,t,i)=>s+t+renderText(value.values[i]),''):Array.isArray(value)?value.map(renderText).join(''):value===context.nothing||value==null?'':String(value);
  const app=fixture(); app.backendVersion='2.4.1';
  for(const connection of ['http','unknown']) for(const status of ['warning','error','success']) {
    app.connection=connection; app.state.last_status=status;
    const output=renderText(app.render());
    assert(output.includes('ha-ops-status-badge'));
    assert(output.includes('data-status-code='+status));
    assert(output.includes('data-connection-state='+connection));
    assert(output.includes('.label='+(connection==='unknown'?'unknown':status==='success'?'done':status)+' data-status-code='+status)); checks++;
  }
  app.connection='http'; app.state.deleted_devices_pending_confirmation=true; app.state.last_status='error';
  assert(renderText(app.render()).includes('.severity=pending decision .label=pending decision data-status-code=pending decision'),'pending status precedence changed');
  app.backendVersion=''; assert(renderText(app.render()).includes('severity="version" .label= data-testid="version-badge"'),'empty version bootstrap changed'); checks++;
  const original=fs.readFileSync(sourcePath,'utf8');
  assert.equal((original.match(/<ha-ops-status-badge\b/g)||[]).length,5,'all five application badge sites share one component');
  assert.equal((original.match(/<vaadin-badge\b/g)||[]).length,1,'native markup belongs only to shared component');
  assert(!original.includes('class="badge')&&!original.includes('class=${`badge'));
  assert(!original.includes('--vaadin-badge-background: #fffbeb'));
  assert(original.includes('--status-background-tint: 10%')&&original.includes('--status-border-tint: 28%'),'preserve distinct version tints');
  checks++;
}
const groups={local,lifetime,final,backup,fences,native,lazyDiff,transport,badges};
(async()=>{const name=process.argv[2];assert(groups[name]);await groups[name]();console.log(JSON.stringify({scenario:name,checks,completeProductionModule:true,rendered:false,result:'passed'}));})().catch(error=>{console.error(error);process.exitCode=1;});
