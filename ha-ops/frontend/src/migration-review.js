import {LitElement,html,css,nothing} from 'lit';
import '@vaadin/button';
import '@vaadin/details';

export class HaOpsMigrationReview extends LitElement {
  static properties={endpoint:{type:String},state:{state:true},model:{state:true},reviewId:{state:true},message:{state:true},busy:{state:true},hiddenPane:{state:true},rowPage:{state:true},content:{state:true},page:{state:true}};
  static styles=css`:host{display:block;min-width:0;max-width:100%;overflow-wrap:anywhere;font:16px system-ui;color:#243443}*{box-sizing:border-box;min-width:0}p,h2,h3,vaadin-details{max-width:100%;overflow-wrap:anywhere}section{padding:20px;margin:16px auto;max-width:1000px;border:1px solid #d6dde4;border-radius:12px;background:white}vaadin-button{margin:6px}vaadin-button[disabled]{background:#eee;color:#8b9299;border:1px solid #d5d9dd;opacity:1}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}article{padding:12px;border-bottom:1px solid #ddd}dl{display:grid;grid-template-columns:minmax(5rem,auto) minmax(0,1fr);gap:5px}dd{margin:0;overflow-wrap:anywhere}.bytes{font:13px monospace}h3{font-size:1rem;overflow-wrap:anywhere}`;
  constructor(){super();this.endpoint='';this.reviewId=null;this.rowPage=0;this.message='';this.busy=false;this.hiddenPane=false;this.content=null;this.page=null;this.generation=0;}
  connectedCallback(){
    super.connectedCallback();
    this.offline=()=>this.invalidate('Connection changed. Start a new explicit review.');
    this.online=()=>this.load();
    window.addEventListener('offline',this.offline);
    window.addEventListener('online',this.online);
    window.addEventListener('ha-ops-migration-reset',this.offline);
    window.addEventListener('ha-ops-migration-refresh',this.online);
    this.load();
  }
  disconnectedCallback(){
    this.invalidate('Review disconnected. Start a new explicit review.');
    window.removeEventListener('offline',this.offline);
    window.removeEventListener('online',this.online);
    window.removeEventListener('ha-ops-migration-reset',this.offline);
    window.removeEventListener('ha-ops-migration-refresh',this.online);
    super.disconnectedCallback();
  }
  invalidate(message){
    ++this.generation;
    this.activeAction=null;
    this.busy=false;
    this.reviewId=null;
    this.message=message;
    this.content=null;
    this.page=null;
    this.rowPage=0;
  }
  async api(route,value){const response=await fetch(this.endpoint+route,{method:value===undefined?'GET':'POST',headers:value===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':this.state?.csrf||''},body:value===undefined?undefined:JSON.stringify(value),cache:'no-store'});const data=await response.json();if(!response.ok||data.ok===false)throw Error(data.message||'Migration review unavailable');return data;}
  async load(){
    this.invalidate('');
    const generation=this.generation;
    this.hiddenPane=false;
    this.state=null;
    this.model=null;
    this.busy=true;
    try{
      const state=await this.api('state');
      if(generation!==this.generation)return;
      this.state=state;
      this.model=state.model;
    }catch(error){if(generation===this.generation)this.message=error.message;}
    finally{if(generation===this.generation)this.busy=false;}
  }
  async act(call){
    if(this.busy)return;
    const action={generation:this.generation};
    this.activeAction=action;
    this.busy=true;
    try{await call(action.generation);}
    catch(error){if(action.generation===this.generation)this.invalidate(error.message);}
    finally{if(this.activeAction===action){this.activeAction=null;this.busy=false;}}
  }
  async review(){await this.act(async generation=>{const result=await this.api('review',{candidate:this.model.candidate,scope:this.model.scope_digest});if(generation!==this.generation)return;this.model=result.model;this.reviewId=result.review_id;this.message='Current complete candidate reviewed. Choose an explicit decision.';});}
  reference(){return {review_id:this.reviewId,candidate:this.model.candidate,scope:this.model.scope_digest};}
  async decide(action){await this.act(async generation=>{const result=await this.api('decision',{...this.reference(),action});if(generation!==this.generation)return;this.reviewId=null;this.state={...this.state,decision:result.decision};this.message=`Explicit ${action} recorded as diagnostic evidence. Migration remains required.`;this.hiddenPane=action==='dismiss';});}
  async open(row,side){
    await this.act(async generation=>{
      const content=await this.api('content-open',{...this.reference(),row,side});
      if(generation!==this.generation)return;
      const page=await this.api('content-page',{view_id:content.view_id,offset:0});
      if(generation!==this.generation)return;
      this.content=content;
      this.page=page;
    });
  }
  async move(offset){await this.act(async generation=>{const page=await this.api('content-page',{view_id:this.content.view_id,offset});if(generation===this.generation)this.page=page;});}
  async closeContent(){await this.act(async generation=>{await this.api('content-close',{});if(generation!==this.generation)return;this.content=null;this.page=null;});}
  attrs(value){return value?`UID ${value.uid}, GID ${value.gid}, mode 0${value.mode.toString(8).padStart(4,'0')} (${value.kind})`:'Unknown / unavailable';}
  bytes(){if(!this.page)return nothing;const raw=Uint8Array.from(atob(this.page.data),c=>c.charCodeAt(0));let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(raw);}catch{text='This byte page is not complete UTF-8 text. Exact hex and base64 are below.';}const hex=Array.from(raw,b=>b.toString(16).padStart(2,'0')).join(' ');const last=this.page.total?Math.floor((this.page.total-1)/16384)*16384:0;return html`<vaadin-details summary="Exact content byte viewer" opened><p data-testid="migration-byte-range">Bytes ${this.page.offset}–${this.page.offset+raw.length} of ${this.page.total}; EOF ${String(this.page.eof)}. SHA256 ${this.page.sha256}</p><vaadin-button ?disabled=${this.busy||this.page.offset===0} @click=${()=>this.move(Math.max(0,this.page.offset-16384))}>Previous bytes</vaadin-button><vaadin-button ?disabled=${this.busy||this.page.eof} @click=${()=>this.move(this.page.offset+raw.length)}>Next bytes</vaadin-button><vaadin-button ?disabled=${this.busy||this.page.offset===last} @click=${()=>this.move(last)}>Last byte page</vaadin-button><vaadin-button ?disabled=${this.busy} @click=${()=>this.closeContent()}>Close content</vaadin-button><vaadin-details summary="Text"><pre>${text}</pre></vaadin-details><vaadin-details summary="Hex bytes" opened><pre class="bytes" data-testid="migration-hex">${hex}</pre></vaadin-details><vaadin-details summary="Lossless base64"><pre>${this.page.data}</pre></vaadin-details></vaadin-details>`;}
  render(){const model=this.model;const enabled=!!this.reviewId&&model?.complete&&!this.busy;return html`<section data-testid="migration-review"><h2>Migration candidate review</h2><p>Migration remains required. Authentication and saved decisions never authorize an operation.</p><p role="status">${this.message||this.state?.status||'Loading saved diagnostics…'}</p>${this.state?.decision?html`<p data-testid="migration-saved-decision">Earlier explicit decision: ${this.state.decision.action}. Diagnostic evidence only; a new current review is required.</p>`:nothing}${this.hiddenPane?html`<p>Review dismissed for this page. It will appear on the next load.</p>`:model?html`<dl><dt>Parent ref</dt><dd>${model.parent_ref_name}</dd><dt>Parent ref (base64)</dt><dd>${model.parent_ref}</dd><dt>Parent commit</dt><dd>${model.parent_oid}</dd><dt>Proposed commit</dt><dd>${model.commit}</dd><dt>Proposed tree</dt><dd>${model.tree}</dd><dt>Exact scope</dt><dd>${model.scope_digest}</dd></dl>${model.consequences.map(text=>html`<p>${text}</p>`)}<vaadin-button ?disabled=${this.busy} @click=${()=>this.review()}>Review current candidate</vaadin-button><vaadin-button ?disabled=${!enabled} @click=${()=>this.decide('approve')}>Approve exact candidate and scope</vaadin-button><vaadin-button ?disabled=${!enabled} @click=${()=>this.decide('refuse')}>Refuse migration</vaadin-button><vaadin-button ?disabled=${!enabled} @click=${()=>this.decide('dismiss')}>Dismiss for this page</vaadin-button><vaadin-details summary="Frozen scope and preparation dispositions"><pre>${JSON.stringify(model.scope,null,2)}</pre></vaadin-details><vaadin-details summary="Required parent directories" opened>${model.parents.map(parent=>html`<article><h3>${parent.name}</h3><p>Byte path (base64): ${parent.path}</p><p>Before: ${parent.before_status}</p><p>After: ${parent.after_status}: ${this.attrs(parent.after)}</p><p>Observed source parent: ${this.attrs(parent.observed_source_metadata)}</p><p>Observed native parent: ${this.attrs(parent.observed_native_metadata)}</p></article>`)}</vaadin-details><h3>All candidate paths: ${model.total}; rows ${Math.min(this.rowPage*25+1,model.total)}–${Math.min((this.rowPage+1)*25,model.total)}</h3><vaadin-button ?disabled=${this.rowPage===0} @click=${()=>this.rowPage--}>Previous paths</vaadin-button><vaadin-button ?disabled=${(this.rowPage+1)*25>=model.total} @click=${()=>this.rowPage++}>Next paths</vaadin-button>${model.rows.slice(this.rowPage*25,(this.rowPage+1)*25).map(row=>html`<article data-testid="migration-row"><h3>${row.action}: ${row.before_name||'∅'} → ${row.after_name||'∅'}</h3>${['before','after'].map(side=>html`<vaadin-details .summary=${side==='before'?'Before content and metadata':'After content and metadata'}><p>Path base64: ${row[side]?.path||'Absent'}</p><p>Git mode: ${row[side]?.mode||'Absent'}; blob: ${row[side]?.oid||'Absent'}</p><p>${row[side+'_metadata_status']}: ${this.attrs(row[side+'_metadata'])}</p>${side==='before'&&row.observed_source_metadata?html`<p>Observed source metadata (not historical Git metadata): ${this.attrs(row.observed_source_metadata)}</p>`:nothing}<vaadin-button ?disabled=${!enabled||!row[side]} @click=${()=>this.open(row.index,side)}>Inspect ${side} bytes</vaadin-button></vaadin-details>`)}</article>`)}${this.bytes()}`:html`<p>Saved candidate unavailable. No decision can be made.</p>`}</section>`;}
}
customElements.define('ha-ops-migration-review',HaOpsMigrationReview);
