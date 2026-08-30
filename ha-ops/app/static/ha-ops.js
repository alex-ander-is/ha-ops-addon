var Ce=globalThis,Ae=Ce.ShadowRoot&&(Ce.ShadyCSS===void 0||Ce.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,ot=Symbol(),mi=new WeakMap,te=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==ot)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o,e=this.t;if(Ae&&t===void 0){let i=e!==void 0&&e.length===1;i&&(t=mi.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&mi.set(e,t))}return t}toString(){return this.cssText}},u=r=>new te(typeof r=="string"?r:r+"",void 0,ot),c=(r,...t)=>{let e=r.length===1?r[0]:t.reduce((i,o,s)=>i+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(o)+r[s+1],r[0]);return new te(e,r,ot)},ke=(r,t)=>{if(Ae)r.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let e of t){let i=document.createElement("style"),o=Ce.litNonce;o!==void 0&&i.setAttribute("nonce",o),i.textContent=e.cssText,r.appendChild(i)}},st=Ae?r=>r:r=>r instanceof CSSStyleSheet?(t=>{let e="";for(let i of t.cssRules)e+=i.cssText;return u(e)})(r):r;var{is:co,defineProperty:ho,getOwnPropertyDescriptor:uo,getOwnPropertyNames:po,getOwnPropertySymbols:fo,getPrototypeOf:mo}=Object,Ee=globalThis,vi=Ee.trustedTypes,vo=vi?vi.emptyScript:"",_o=Ee.reactiveElementPolyfillSupport,ue=(r,t)=>r,nt={toAttribute(r,t){switch(t){case Boolean:r=r?vo:null;break;case Object:case Array:r=r==null?r:JSON.stringify(r)}return r},fromAttribute(r,t){let e=r;switch(t){case Boolean:e=r!==null;break;case Number:e=r===null?null:Number(r);break;case Object:case Array:try{e=JSON.parse(r)}catch{e=null}}return e}},Se=(r,t)=>!co(r,t),_i={attribute:!0,type:String,converter:nt,reflect:!1,useDefault:!1,hasChanged:Se};Symbol.metadata??=Symbol("metadata"),Ee.litPropertyMetadata??=new WeakMap;var $=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=_i){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){let i=Symbol(),o=this.getPropertyDescriptor(t,i,e);o!==void 0&&ho(this.prototype,t,o)}}static getPropertyDescriptor(t,e,i){let{get:o,set:s}=uo(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:o,set(n){let a=o?.call(this);s?.call(this,n),this.requestUpdate(t,a,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??_i}static _$Ei(){if(this.hasOwnProperty(ue("elementProperties")))return;let t=mo(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(ue("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(ue("properties"))){let e=this.properties,i=[...po(e),...fo(e)];for(let o of i)this.createProperty(o,e[o])}let t=this[Symbol.metadata];if(t!==null){let e=litPropertyMetadata.get(t);if(e!==void 0)for(let[i,o]of e)this.elementProperties.set(i,o)}this._$Eh=new Map;for(let[e,i]of this.elementProperties){let o=this._$Eu(e,i);o!==void 0&&this._$Eh.set(o,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){let e=[];if(Array.isArray(t)){let i=new Set(t.flat(1/0).reverse());for(let o of i)e.unshift(st(o))}else t!==void 0&&e.push(st(t));return e}static _$Eu(t,e){let i=e.attribute;return i===!1?void 0:typeof i=="string"?i:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){let t=new Map,e=this.constructor.elementProperties;for(let i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){let t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return ke(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){let i=this.constructor.elementProperties.get(t),o=this.constructor._$Eu(t,i);if(o!==void 0&&i.reflect===!0){let s=(i.converter?.toAttribute!==void 0?i.converter:nt).toAttribute(e,i.type);this._$Em=t,s==null?this.removeAttribute(o):this.setAttribute(o,s),this._$Em=null}}_$AK(t,e){let i=this.constructor,o=i._$Eh.get(t);if(o!==void 0&&this._$Em!==o){let s=i.getPropertyOptions(o),n=typeof s.converter=="function"?{fromAttribute:s.converter}:s.converter?.fromAttribute!==void 0?s.converter:nt;this._$Em=o;let a=n.fromAttribute(e,s.type);this[o]=a??this._$Ej?.get(o)??a,this._$Em=null}}requestUpdate(t,e,i,o=!1,s){if(t!==void 0){let n=this.constructor;if(o===!1&&(s=this[t]),i??=n.getPropertyOptions(t),!((i.hasChanged??Se)(s,e)||i.useDefault&&i.reflect&&s===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,i))))return;this.C(t,e,i)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:o,wrapped:s},n){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),s!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),o===!0&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[o,s]of this._$Ep)this[o]=s;this._$Ep=void 0}let i=this.constructor.elementProperties;if(i.size>0)for(let[o,s]of i){let{wrapped:n}=s,a=this[o];n!==!0||this._$AL.has(o)||a===void 0||this.C(o,void 0,s,a)}}let t=!1,e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(i=>i.hostUpdate?.()),this.update(e)):this._$EM()}catch(i){throw t=!1,this._$EM(),i}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(t){}firstUpdated(t){}};$.elementStyles=[],$.shadowRootOptions={mode:"open"},$[ue("elementProperties")]=new Map,$[ue("finalized")]=new Map,_o?.({ReactiveElement:$}),(Ee.reactiveElementVersions??=[]).push("2.1.2");var pt=globalThis,gi=r=>r,Me=pt.trustedTypes,bi=Me?Me.createPolicy("lit-html",{createHTML:r=>r}):void 0,ki="$lit$",P=`lit$${Math.random().toFixed(9).slice(2)}$`,Ei="?"+P,go=`<${Ei}>`,X=document,fe=()=>X.createComment(""),me=r=>r===null||typeof r!="object"&&typeof r!="function",ft=Array.isArray,bo=r=>ft(r)||typeof r?.[Symbol.iterator]=="function",at=`[ 	
\f\r]`,pe=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,yi=/-->/g,xi=/>/g,W=RegExp(`>|${at}(?:([^\\s"'>=/]+)(${at}*=${at}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),wi=/'/g,Ci=/"/g,Si=/^(?:script|style|textarea|title)$/i,mt=r=>(t,...e)=>({_$litType$:r,strings:t,values:e}),m=mt(1),Ks=mt(2),Ws=mt(3),Y=Symbol.for("lit-noChange"),g=Symbol.for("lit-nothing"),Ai=new WeakMap,G=X.createTreeWalker(X,129);function Mi(r,t){if(!ft(r)||!r.hasOwnProperty("raw"))throw Error("invalid template strings array");return bi!==void 0?bi.createHTML(t):t}var yo=(r,t)=>{let e=r.length-1,i=[],o,s=t===2?"<svg>":t===3?"<math>":"",n=pe;for(let a=0;a<e;a++){let l=r[a],d,f,h=-1,w=0;for(;w<l.length&&(n.lastIndex=w,f=n.exec(l),f!==null);)w=n.lastIndex,n===pe?f[1]==="!--"?n=yi:f[1]!==void 0?n=xi:f[2]!==void 0?(Si.test(f[2])&&(o=RegExp("</"+f[2],"g")),n=W):f[3]!==void 0&&(n=W):n===W?f[0]===">"?(n=o??pe,h=-1):f[1]===void 0?h=-2:(h=n.lastIndex-f[2].length,d=f[1],n=f[3]===void 0?W:f[3]==='"'?Ci:wi):n===Ci||n===wi?n=W:n===yi||n===xi?n=pe:(n=W,o=void 0);let C=n===W&&r[a+1].startsWith("/>")?" ":"";s+=n===pe?l+go:h>=0?(i.push(d),l.slice(0,h)+ki+l.slice(h)+P+C):l+P+(h===-2?a:C)}return[Mi(r,s+(r[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),i]},ve=class r{constructor({strings:t,_$litType$:e},i){let o;this.parts=[];let s=0,n=0,a=t.length-1,l=this.parts,[d,f]=yo(t,e);if(this.el=r.createElement(d,i),G.currentNode=this.el.content,e===2||e===3){let h=this.el.content.firstChild;h.replaceWith(...h.childNodes)}for(;(o=G.nextNode())!==null&&l.length<a;){if(o.nodeType===1){if(o.hasAttributes())for(let h of o.getAttributeNames())if(h.endsWith(ki)){let w=f[n++],C=o.getAttribute(h).split(P),ee=/([.?@])?(.*)/.exec(w);l.push({type:1,index:s,name:ee[2],strings:C,ctor:ee[1]==="."?dt:ee[1]==="?"?ct:ee[1]==="@"?ht:re}),o.removeAttribute(h)}else h.startsWith(P)&&(l.push({type:6,index:s}),o.removeAttribute(h));if(Si.test(o.tagName)){let h=o.textContent.split(P),w=h.length-1;if(w>0){o.textContent=Me?Me.emptyScript:"";for(let C=0;C<w;C++)o.append(h[C],fe()),G.nextNode(),l.push({type:2,index:++s});o.append(h[w],fe())}}}else if(o.nodeType===8)if(o.data===Ei)l.push({type:2,index:s});else{let h=-1;for(;(h=o.data.indexOf(P,h+1))!==-1;)l.push({type:7,index:s}),h+=P.length-1}s++}}static createElement(t,e){let i=X.createElement("template");return i.innerHTML=t,i}};function ie(r,t,e=r,i){if(t===Y)return t;let o=i!==void 0?e._$Co?.[i]:e._$Cl,s=me(t)?void 0:t._$litDirective$;return o?.constructor!==s&&(o?._$AO?.(!1),s===void 0?o=void 0:(o=new s(r),o._$AT(r,e,i)),i!==void 0?(e._$Co??=[])[i]=o:e._$Cl=o),o!==void 0&&(t=ie(r,o._$AS(r,t.values),o,i)),t}var lt=class{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){let{el:{content:e},parts:i}=this._$AD,o=(t?.creationScope??X).importNode(e,!0);G.currentNode=o;let s=G.nextNode(),n=0,a=0,l=i[0];for(;l!==void 0;){if(n===l.index){let d;l.type===2?d=new _e(s,s.nextSibling,this,t):l.type===1?d=new l.ctor(s,l.name,l.strings,this,t):l.type===6&&(d=new ut(s,this,t)),this._$AV.push(d),l=i[++a]}n!==l?.index&&(s=G.nextNode(),n++)}return G.currentNode=X,o}p(t){let e=0;for(let i of this._$AV)i!==void 0&&(i.strings!==void 0?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}},_e=class r{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,o){this.type=2,this._$AH=g,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=o,this._$Cv=o?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode,e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=ie(this,t,e),me(t)?t===g||t==null||t===""?(this._$AH!==g&&this._$AR(),this._$AH=g):t!==this._$AH&&t!==Y&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):bo(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==g&&me(this._$AH)?this._$AA.nextSibling.data=t:this.T(X.createTextNode(t)),this._$AH=t}$(t){let{values:e,_$litType$:i}=t,o=typeof i=="number"?this._$AC(t):(i.el===void 0&&(i.el=ve.createElement(Mi(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===o)this._$AH.p(e);else{let s=new lt(o,this),n=s.u(this.options);s.p(e),this.T(n),this._$AH=s}}_$AC(t){let e=Ai.get(t.strings);return e===void 0&&Ai.set(t.strings,e=new ve(t)),e}k(t){ft(this._$AH)||(this._$AH=[],this._$AR());let e=this._$AH,i,o=0;for(let s of t)o===e.length?e.push(i=new r(this.O(fe()),this.O(fe()),this,this.options)):i=e[o],i._$AI(s),o++;o<e.length&&(this._$AR(i&&i._$AB.nextSibling,o),e.length=o)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){let i=gi(t).nextSibling;gi(t).remove(),t=i}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}},re=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,o,s){this.type=1,this._$AH=g,this._$AN=void 0,this.element=t,this.name=e,this._$AM=o,this.options=s,i.length>2||i[0]!==""||i[1]!==""?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=g}_$AI(t,e=this,i,o){let s=this.strings,n=!1;if(s===void 0)t=ie(this,t,e,0),n=!me(t)||t!==this._$AH&&t!==Y,n&&(this._$AH=t);else{let a=t,l,d;for(t=s[0],l=0;l<s.length-1;l++)d=ie(this,a[i+l],e,l),d===Y&&(d=this._$AH[l]),n||=!me(d)||d!==this._$AH[l],d===g?t=g:t!==g&&(t+=(d??"")+s[l+1]),this._$AH[l]=d}n&&!o&&this.j(t)}j(t){t===g?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}},dt=class extends re{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===g?void 0:t}},ct=class extends re{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==g)}},ht=class extends re{constructor(t,e,i,o,s){super(t,e,i,o,s),this.type=5}_$AI(t,e=this){if((t=ie(this,t,e,0)??g)===Y)return;let i=this._$AH,o=t===g&&i!==g||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,s=t!==g&&(i===g||o);o&&this.element.removeEventListener(this.name,this,i),s&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}},ut=class{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){ie(this,t)}};var xo=pt.litHtmlPolyfillSupport;xo?.(ve,_e),(pt.litHtmlVersions??=[]).push("3.3.3");var Ti=(r,t,e)=>{let i=e?.renderBefore??t,o=i._$litPart$;if(o===void 0){let s=e?.renderBefore??null;i._$litPart$=o=new _e(t.insertBefore(fe(),s),s,void 0,e??{})}return o._$AI(r),o};var vt=globalThis,p=class extends ${constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){let e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=Ti(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return Y}};p._$litElement$=!0,p.finalized=!0,vt.litElementHydrateSupport?.({LitElement:p});var wo=vt.litElementPolyfillSupport;wo?.({LitElement:p});(vt.litElementVersions??=[]).push("4.2.2");window.Vaadin||={};window.Vaadin.featureFlags||={};function Co(r){return r.replace(/-[a-z]/gu,t=>t[1].toUpperCase())}var O={};function v(r,t="25.2.9"){if(Object.defineProperty(r,"version",{get(){return t}}),r.experimental){let i=typeof r.experimental=="string"?r.experimental:`${Co(r.is.split("-").slice(1).join("-"))}Component`;if(!window.Vaadin.featureFlags[i]&&!O[i]){O[i]=new Set,O[i].add(r),Object.defineProperty(window.Vaadin.featureFlags,i,{get(){return O[i].size===0},set(o){o&&O[i].size>0&&(O[i].forEach(s=>{customElements.define(s.is,s)}),O[i].clear())}});return}else if(O[i]){O[i].add(r);return}}let e=customElements.get(r.is);if(!e)customElements.define(r.is,r);else{let i=e.version;i&&r.version&&i===r.version?console.warn(`The component ${r.is} has been loaded twice`):console.error(`Tried to define ${r.is} version ${r.version} when version ${e.version} is already in use. Something will probably break.`)}}var Ao=/\/\*[\*!]\s+vaadin-dev-mode:start([\s\S]*)vaadin-dev-mode:end\s+\*\*\//i,Te=window.Vaadin&&window.Vaadin.Flow&&window.Vaadin.Flow.clients;function ko(){function r(){return!0}return Ii(r)}function Eo(){try{return So()?!0:Mo()?Te?!To():!ko():!1}catch{return!1}}function So(){return localStorage.getItem("vaadin.developmentmode.force")}function Mo(){return["localhost","127.0.0.1"].indexOf(window.location.hostname)>=0}function To(){return!!(Te&&Object.keys(Te).map(t=>Te[t]).filter(t=>t.productionMode).length>0)}function Ii(r,t){if(typeof r!="function")return;let e=Ao.exec(r.toString());if(e)try{r=new Function(e[1])}catch(i){console.log("vaadin-development-mode-detector: uncommentAndRun() failed",i)}return r(t)}window.Vaadin=window.Vaadin||{};var _t=function(r,t){if(window.Vaadin.developmentMode)return Ii(r,t)};window.Vaadin.developmentMode===void 0&&(window.Vaadin.developmentMode=Eo());function Io(){}var $i=function(){if(typeof _t=="function")return _t(Io)};var Oi=0,Li=0,oe=[],gt=!1;function $o(){gt=!1;let r=oe.length;for(let t=0;t<r;t++){let e=oe[t];if(e)try{e()}catch(i){setTimeout(()=>{throw i})}}oe.splice(0,r),Li+=r}var Ni={after(r){return{run(t){return window.setTimeout(t,r)},cancel(t){window.clearTimeout(t)}}},run(r,t){return window.setTimeout(r,t)},cancel(r){window.clearTimeout(r)}};var Pi={run(r){return window.requestAnimationFrame(r)},cancel(r){window.cancelAnimationFrame(r)}};var Di={run(r){return window.requestIdleCallback?window.requestIdleCallback(r):window.setTimeout(r,16)},cancel(r){window.cancelIdleCallback?window.cancelIdleCallback(r):window.clearTimeout(r)}};var Ri={run(r){gt||(gt=!0,queueMicrotask(()=>$o())),oe.push(r);let t=Oi;return Oi+=1,t},cancel(r){let t=r-Li;if(t>=0){if(!oe[t])throw new Error(`invalid async handle: ${r}`);oe[t]=null}}};var bt=new Set,D=class r{static debounce(t,e,i){return t instanceof r?t._cancelAsync():t=new r,t.setConfig(e,i),t}constructor(){this._asyncModule=null,this._callback=null,this._timer=null}setConfig(t,e){this._asyncModule=t,this._callback=e,this._timer=this._asyncModule.run(()=>{this._timer=null,bt.delete(this),this._callback()})}cancel(){this.isActive()&&(this._cancelAsync(),bt.delete(this))}_cancelAsync(){this.isActive()&&(this._asyncModule.cancel(this._timer),this._timer=null)}flush(){this.isActive()&&(this.cancel(),this._callback())}isActive(){return this._timer!=null}};function Bi(r){bt.add(r)}var L=[];function yt(r,t,e=r.getAttribute("dir")){t?r.setAttribute("dir",t):e!=null&&r.removeAttribute("dir")}function xt(){return document.documentElement.getAttribute("dir")}function Oo(){let r=xt();L.forEach(t=>{yt(t,r)})}var Lo=new MutationObserver(Oo);Lo.observe(document.documentElement,{attributes:!0,attributeFilter:["dir"]});var S=r=>class extends r{static get properties(){return{dir:{type:String,value:"",reflectToAttribute:!0,converter:{fromAttribute:e=>e||"",toAttribute:e=>e===""?null:e}}}}get __isRTL(){return this.getAttribute("dir")==="rtl"}connectedCallback(){super.connectedCallback(),(!this.hasAttribute("dir")||this.__restoreSubscription)&&(this.__subscribe(),yt(this,xt(),null))}attributeChangedCallback(e,i,o){if(super.attributeChangedCallback(e,i,o),e!=="dir")return;let s=xt(),n=o===s&&L.indexOf(this)===-1,a=!o&&i&&L.indexOf(this)===-1;n||a?(this.__subscribe(),yt(this,s,o)):o!==s&&i===s&&this.__unsubscribe()}disconnectedCallback(){super.disconnectedCallback(),this.__restoreSubscription=L.includes(this),this.__unsubscribe()}_valueToNodeAttribute(e,i,o){o==="dir"&&i===""&&!e.hasAttribute("dir")||super._valueToNodeAttribute(e,i,o)}_attributeToProperty(e,i,o){e==="dir"&&!i?this.dir="":super._attributeToProperty(e,i,o)}__subscribe(){L.includes(this)||L.push(this)}__unsubscribe(){L.includes(this)&&L.splice(L.indexOf(this),1)}};window.Vaadin||(window.Vaadin={});window.Vaadin.registrations||(window.Vaadin.registrations=[]);window.Vaadin.developmentModeCallback||(window.Vaadin.developmentModeCallback={});window.Vaadin.developmentModeCallback["vaadin-usage-statistics"]=function(){$i()};var wt,Fi=new Set,E=r=>class extends S(r){static _ensureRegistrations(){let{is:e}=this;if(e&&!Fi.has(e)){window.Vaadin.registrations.push(this),Fi.add(e);let i=window.Vaadin.developmentModeCallback;i&&(wt=D.debounce(wt,Di,()=>{i["vaadin-usage-statistics"]()}),Bi(wt))}}constructor(){super(),document.doctype===null&&console.warn('Vaadin components require the "standards mode" declaration. Please add <!DOCTYPE html> to the HTML document.'),this.constructor._ensureRegistrations()}};var zi=new WeakMap;function No(r,t){let e=t;for(;e;){if(zi.get(e)===r)return!0;e=Object.getPrototypeOf(e)}return!1}function x(r){return t=>{if(No(r,t))return t;let e=r(t);return zi.set(e,r),e}}function ji(r,t){return r.split(".").reduce((e,i)=>e?e[i]:void 0,t)}function Vi(r,t,e){let i=r.split("."),o=i.pop(),s=i.reduce((n,a)=>n[a],e);s[o]=t}var Ct={},Po=/([A-Z])/gu;function Ui(r){return Ct[r]||(Ct[r]=r.replace(Po,"-$1").toLowerCase()),Ct[r]}function Hi(r){return r[0].toUpperCase()+r.substring(1)}function At(r){let[t,e]=r.split("("),i=e.replace(")","").split(",").map(o=>o.trim());return{method:t,observerProps:i}}function kt(r,t){return Object.prototype.hasOwnProperty.call(r,t)||(r[t]=new Map(r[t])),r[t]}var Do=r=>{class t extends r{static enabledWarnings=[];static createProperty(i,o){[String,Boolean,Number,Array].includes(o)&&(o={type:o}),o?.reflectToAttribute&&(o.reflect=!0),super.createProperty(i,o)}static getOrCreateMap(i){return kt(this,i)}static finalize(){if(window.litIssuedWarnings&&(window.litIssuedWarnings.add("no-override-create-property"),window.litIssuedWarnings.add("no-override-get-property-descriptor")),super.finalize(),Array.isArray(this.observers)){let i=this.getOrCreateMap("__complexObservers");this.observers.forEach(o=>{let{method:s,observerProps:n}=At(o);i.set(s,n)})}}static addCheckedInitializer(i){super.addInitializer(o=>{o instanceof this&&i(o)})}static getPropertyDescriptor(i,o,s){let n=super.getPropertyDescriptor(i,o,s),a=n;if(this.getOrCreateMap("__propKeys").set(i,o),s.sync&&(a={get:n.get,set(l){let d=this[i];Se(l,d)&&(this[o]=l,this.requestUpdate(i,d,s),this.hasUpdated&&this.performUpdate())},configurable:!0,enumerable:!0}),s.readOnly){let l=a.set;this.addCheckedInitializer(d=>{d[`_set${Hi(i)}`]=function(f){l.call(d,f)}}),a={get:a.get,set(){},configurable:!0,enumerable:!0}}if("value"in s&&this.addCheckedInitializer(l=>{let d=typeof s.value=="function"?s.value.call(l):s.value;s.readOnly?l[`_set${Hi(i)}`](d):l[i]=d}),s.observer){let l=s.observer;this.getOrCreateMap("__observers").set(i,l),this.addCheckedInitializer(d=>{d[l]||console.warn(`observer method ${l} not defined`)})}if(s.notify){if(!this.__notifyProps)this.__notifyProps=new Set;else if(!this.hasOwnProperty("__notifyProps")){let l=this.__notifyProps;this.__notifyProps=new Set(l)}this.__notifyProps.add(i)}if(s.computed){let l=`__assignComputed${i}`,d=At(s.computed);this.prototype[l]=function(...f){this[i]=this[d.method](...f)},this.getOrCreateMap("__computedObservers").set(l,d.observerProps)}return s.attribute||(s.attribute=Ui(i)),a}static get polylitConfig(){return{asyncFirstRender:!1}}connectedCallback(){super.connectedCallback();let{polylitConfig:i}=this.constructor;!this.hasUpdated&&!i.asyncFirstRender&&this.performUpdate()}firstUpdated(){super.firstUpdated(),this.$||(this.$={}),this.renderRoot.querySelectorAll("[id]").forEach(i=>{this.$[i.id]=i})}ready(){}willUpdate(i){this.constructor.__computedObservers&&this.__runComplexObservers(i,this.constructor.__computedObservers)}updated(i){let o=this.__isReadyInvoked;this.__isReadyInvoked=!0,this.constructor.__observers&&this.__runObservers(i,this.constructor.__observers),this.constructor.__complexObservers&&this.__runComplexObservers(i,this.constructor.__complexObservers),this.__dynamicPropertyObservers&&this.__runDynamicObservers(i,this.__dynamicPropertyObservers),this.__dynamicMethodObservers&&this.__runComplexObservers(i,this.__dynamicMethodObservers),this.constructor.__notifyProps&&this.__runNotifyProps(i,this.constructor.__notifyProps),o||this.ready()}setProperties(i){Object.entries(i).forEach(([o,s])=>{let n=this.constructor.__propKeys.get(o),a=this[n];this[n]=s,this.requestUpdate(o,a)}),this.hasUpdated&&this.performUpdate()}_createMethodObserver(i){let o=kt(this,"__dynamicMethodObservers"),{method:s,observerProps:n}=At(i);o.set(s,n)}_createPropertyObserver(i,o){kt(this,"__dynamicPropertyObservers").set(o,i)}__runComplexObservers(i,o){o.forEach((s,n)=>{s.some(a=>i.has(a))&&(this[n]?this[n](...s.map(a=>this[a])):console.warn(`observer method ${n} not defined`))})}__runDynamicObservers(i,o){o.forEach((s,n)=>{i.has(s)&&this[n]&&this[n](this[s],i.get(s))})}__runObservers(i,o){i.forEach((s,n)=>{let a=o.get(n);a!==void 0&&this[a]&&this[a](this[n],s)})}__runNotifyProps(i,o){i.forEach((s,n)=>{o.has(n)&&this.dispatchEvent(new CustomEvent(`${Ui(n)}-changed`,{detail:{value:this[n]}}))})}_get(i,o){return ji(i,o)}_set(i,o,s){Vi(i,o,s)}}return t},_=x(Do);function qi(r){let t=[];for(;r;){if(r.nodeType===Node.DOCUMENT_NODE){t.push(r);break}if(r.nodeType===Node.DOCUMENT_FRAGMENT_NODE){t.push(r),r=r.host;continue}if(r.assignedSlot){r=r.assignedSlot;continue}r=r.parentNode}return t}function Ie(r){return r?new Set(r.split(" ")):new Set}function ge(r){return r?[...r].join(" "):""}function Et(r,t,e){let i=Ie(r.getAttribute(t));i.add(e),r.setAttribute(t,ge(i))}function Ki(r,t,e){let i=Ie(r.getAttribute(t));if(i.delete(e),i.size===0){r.removeAttribute(t);return}r.setAttribute(t,ge(i))}function Wi(r){return r.nodeType===Node.TEXT_NODE&&r.textContent.trim()===""}var R=class{constructor(t,e,i={}){this.target=t,this.callback=e,this.forceInitial=i.forceInitial,this._storedNodes=[],this._isSlot=t instanceof HTMLSlotElement,this._connected=!1,this._scheduled=!1,this._boundSchedule=()=>{this._schedule()},this.connect(),i.syncInitial?this.flush():this._schedule()}connect(){this.target.addEventListener("slotchange",this._boundSchedule),this._connected=!0}disconnect(){this.target.removeEventListener("slotchange",this._boundSchedule),this._connected=!1}_schedule(){this._scheduled||(this._scheduled=!0,queueMicrotask(()=>{this._scheduled&&this.flush()}))}flush(){this._connected&&(this._scheduled=!1,this._processNodes())}_collectNodes(){let t=this._isSlot?[this.target]:[...this.target.querySelectorAll("slot")];return[...new Set(t.flatMap(e=>e.assignedNodes({flatten:!0})))]}_groupNodesBySlot(t){let e=new Map;return t.forEach(i=>{let o=i.assignedSlot;e.set(o,e.get(o)??[]),e.get(o).push(i)}),e}_collectMovedNodes(t){let e=this._groupNodesBySlot(t),i=this._groupNodesBySlot(this._storedNodes),o=[];return e.forEach((s,n)=>{let a=i.get(n)||[];new Set(a).difference(new Set(s)).size>0||a.forEach((l,d)=>{s.indexOf(l)!==d&&o.push(l)})}),o}_processNodes(){let t=this._collectNodes(),e=t.filter(s=>!this._storedNodes.includes(s)),i=this._storedNodes.filter(s=>!t.includes(s)),o=this._collectMovedNodes(t);(e.length||i.length||o.length||this.forceInitial)&&this.callback({addedNodes:e,currentNodes:t,movedNodes:o,removedNodes:i}),this.forceInitial&&(this.forceInitial=!1),this._storedNodes=t}};var Ro=0;function se(){return Ro++}var A=class extends EventTarget{static generateId(t,e="default"){return`${e}-${t.localName}-${se()}`}constructor(t,e,i,o={}){super();let{initializer:s,multiple:n,observe:a,useUniqueId:l,uniqueIdPrefix:d}=o;this.host=t,this.slotName=e,this.tagName=i,this.observe=typeof a=="boolean"?a:!0,this.multiple=typeof n=="boolean"?n:!1,this.slotInitializer=s,n&&(this.nodes=[]),l&&(this.defaultId=this.constructor.generateId(t,d||e))}hostConnected(){this.initialized||(this.multiple?this.initMultiple():this.initSingle(),this.observe&&this.observeSlot(),this.initialized=!0)}initSingle(){let t=this.getSlotChild();t?(this.node=t,this.initAddedNode(t)):(t=this.attachDefaultNode(),this.initNode(t))}initMultiple(){let t=this.getSlotChildren();if(t.length===0){let e=this.attachDefaultNode();e&&(this.nodes=[e],this.initNode(e))}else this.nodes=t,t.forEach(e=>{this.initAddedNode(e)})}attachDefaultNode(){let{host:t,slotName:e,tagName:i}=this,o=this.defaultNode;return!o&&i&&(o=document.createElement(i),o instanceof Element&&(e!==""&&o.setAttribute("slot",e),this.defaultNode=o)),o&&(this.node=o,t.appendChild(o)),o}getSlotChildren(){let{slotName:t}=this;return Array.from(this.host.childNodes).filter(e=>e.nodeType===Node.ELEMENT_NODE&&e.hasAttribute("data-slot-ignore")?!1:e.nodeType===Node.ELEMENT_NODE&&e.slot===t||e.nodeType===Node.TEXT_NODE&&e.textContent.trim()&&t==="")}getSlotChild(){return this.getSlotChildren()[0]}initNode(t){let{slotInitializer:e}=this;e&&e(t,this.host)}initCustomNode(t){}teardownNode(t){}initAddedNode(t){t!==this.defaultNode&&(this.initCustomNode(t),this.initNode(t))}observeSlot(){let{slotName:t}=this,e=t===""?"slot:not([name])":`slot[name=${t}]`,i=this.host.shadowRoot.querySelector(e);this.__slotObserver=new R(i,({addedNodes:o,removedNodes:s})=>{let n=this.multiple?this.nodes:[this.node],a=o.filter(l=>!Wi(l)&&!n.includes(l)&&!(l.nodeType===Node.ELEMENT_NODE&&l.hasAttribute("data-slot-ignore")));s.length&&(this.nodes=n.filter(l=>!s.includes(l)),s.forEach(l=>{this.teardownNode(l)})),a?.length>0&&(this.multiple?(this.defaultNode&&this.defaultNode.remove(),this.nodes=[...n,...a].filter(l=>l!==this.defaultNode),a.forEach(l=>{this.initAddedNode(l)})):(this.node&&this.node.remove(),this.node=a[0],this.initAddedNode(this.node)))})}};var I=class extends A{constructor(t){super(t,"tooltip"),this.setTarget(t),this.__onContentChange=this.__onContentChange.bind(this)}initCustomNode(t){t.target=this.target,this.ariaTarget!==void 0&&(t.ariaTarget=this.ariaTarget),this.context!==void 0&&(t.context=this.context),this.manual!==void 0&&(t.manual=this.manual),this.position!==void 0&&(t._position=this.position),this.shouldShow!==void 0&&(t.shouldShow=this.shouldShow),this.manual||this.host.setAttribute("has-tooltip",""),this.__notifyChange(t),t.addEventListener("content-changed",this.__onContentChange)}teardownNode(t){this.manual||this.host.removeAttribute("has-tooltip"),t.removeEventListener("content-changed",this.__onContentChange),this.__notifyChange(null)}setAriaTarget(t){this.ariaTarget=t;let e=this.node;e&&(e.ariaTarget=t)}setContext(t){this.context=t;let e=this.node;e&&(e.context=t)}setManual(t){this.manual=t;let e=this.node;e&&(e.manual=t)}setPosition(t){this.position=t;let e=this.node;e&&(e._position=t)}setShouldShow(t){this.shouldShow=t;let e=this.node;e&&(e.shouldShow=t)}setTarget(t){this.target=t;let e=this.node;e&&(e.target=t)}open(t){let e=this.node;e?.isConnected&&e._stateController.open(t)}close(t){let e=this.node;e&&e._stateController.close(t)}__onContentChange(t){this.__notifyChange(t.target)}__notifyChange(t){this.dispatchEvent(new CustomEvent("tooltip-changed",{detail:{node:t}}))}};function $e(r){try{CSS.registerProperty(r)}catch(t){if(t instanceof DOMException&&t.name==="InvalidModificationError")console.warn(`The CSS property ${r.name} has already been registered.`);else throw t}}var Gi=(r,...t)=>{let e=document.createElement("style");e.id=r,e.textContent=t.map(i=>i.toString()).join(`
`),document.head.insertAdjacentElement("afterbegin",e)};var Oe=class r extends EventTarget{#r;#e=new Set;#t;#i=!1;constructor(t){super(),this.#r=t,this.#t=new CSSStyleSheet}#s(t){let{propertyName:e}=t;this.#e.has(e)&&this.dispatchEvent(new CustomEvent("property-changed",{detail:{propertyName:e}}))}observe(t){this.connect(),!this.#e.has(t)&&(this.#e.add(t),this.#t.replaceSync(`
      :root::before, :host::before {
        content: '' !important;
        position: absolute !important;
        top: -9999px !important;
        left: -9999px !important;
        visibility: hidden !important;
        transition: 1ms allow-discrete step-end !important;
        transition-property: ${[...this.#e].join(", ")} !important;
      }
    `))}connect(){this.#i||(this.#r.adoptedStyleSheets.unshift(this.#t),this.#o.addEventListener("transitionstart",t=>this.#s(t)),this.#o.addEventListener("transitionend",t=>this.#s(t)),this.#i=!0)}disconnect(){this.#e.clear(),this.#r.adoptedStyleSheets=this.#r.adoptedStyleSheets.filter(t=>t!==this.#t),this.#o.removeEventListener("transitionstart",this.#s),this.#o.removeEventListener("transitionend",this.#s),this.#i=!1}get#o(){return this.#r.documentElement??this.#r.host}static for(t){return t.__cssPropertyObserver||=new r(t),t.__cssPropertyObserver}};function Bo(r){let{baseStyles:t,themeStyles:e,elementStyles:i,lumoInjector:o}=r.constructor,s=r.__lumoStyleSheet;return s?[...o.includeBaseStyles?t??i:[],s,...e??[]]:i}function St(r){ke(r.shadowRoot,Bo(r))}function Mt(r,t){r.__lumoStyleSheet=t,St(r)}function Le(r){r.__lumoStyleSheet=void 0,St(r)}var Xi=new Set;function Tt(r){Xi.has(r)||(Xi.add(r),console.warn(r))}var Yi=new WeakMap;function Zi(r){try{return r.media.mediaText}catch{return Tt('[LumoInjector] Browser denied to access property "mediaText" for some CSS rules, so they were skipped.'),""}}function Fo(r){try{return r.cssRules}catch{return Tt('[LumoInjector] Browser denied to access property "cssRules" for some CSS stylesheets, so they were skipped.'),[]}}function Ji(r,t={tags:new Map,modules:new Map}){for(let e of Fo(r)){if(e instanceof CSSImportRule){let i=Zi(e);i.startsWith("lumo_")?t.modules.set(i,[...e.styleSheet.cssRules]):Ji(e.styleSheet,t);continue}if(e instanceof CSSMediaRule){let i=Zi(e);i.startsWith("lumo_")&&t.modules.set(i,[...e.cssRules]);continue}if(e instanceof CSSStyleRule&&e.cssText.includes("-inject")){for(let i of e.style){let o=i.match(/^--_lumo-(.*)-inject-modules$/u)?.[1];if(!o)continue;let s=e.style.getPropertyValue(i);t.tags.set(o,s.split(",").map(n=>n.trim().replace(/'|"/gu,"")))}continue}}return t}function Qi(r){let t=new Map,e=new Map;for(let i of r){let o=Yi.get(i);o||(o=Ji(i),Yi.set(i,o)),t=new Map([...t,...o.tags]),e=new Map([...e,...o.modules])}return{tags:t,modules:e}}function It(r){return`--_lumo-${r.is}-inject`}var Ne=class{#r;#e;#t=new Map;#i=new Map;constructor(t=document){this.#r=t,this.handlePropertyChange=this.handlePropertyChange.bind(this),this.#e=Oe.for(t),this.#e.addEventListener("property-changed",this.handlePropertyChange)}disconnect(){this.#e.removeEventListener("property-changed",this.handlePropertyChange),this.#t.clear(),this.#i.values().forEach(t=>t.forEach(Le))}forceUpdate(){for(let t of this.#t.keys())this.#o(t)}componentConnected(t){let{lumoInjector:e}=t.constructor,{is:i}=e;this.#i.set(i,this.#i.get(i)??new Set),this.#i.get(i).add(t);let o=this.#t.get(i);if(o){o.cssRules.length>0&&Mt(t,o);return}this.#s(i);let s=It(e);this.#e.observe(s)}componentDisconnected(t){let{is:e}=t.constructor.lumoInjector;this.#i.get(e)?.delete(t),Le(t)}handlePropertyChange(t){let{propertyName:e}=t.detail,i=e.match(/^--_lumo-(.*)-inject$/u)?.[1];i&&this.#o(i)}#s(t){this.#t.set(t,new CSSStyleSheet),this.#o(t)}#o(t){let{tags:e,modules:i}=Qi(this.#n),o=(e.get(t)??[]).flatMap(n=>i.get(n)??[]).map(n=>n.cssText).join(`
`),s=this.#t.get(t);s.replaceSync(o),this.#i.get(t)?.forEach(n=>{o?Mt(n,s):Le(n)})}get#n(){let t=new Set;for(let e of[this.#r,document])t=t.union(new Set(e.styleSheets)),t=t.union(new Set(e.adoptedStyleSheets));return[...t]}};var er=new Set;function tr(r){let t=r.getRootNode();return t.host&&t.host.constructor.version?tr(t.host):t}var b=r=>class extends r{static finalize(){super.finalize();let e=It(this.lumoInjector);this.is&&!er.has(e)&&(er.add(e),$e({name:e,syntax:"<number>",inherits:!0,initialValue:"0"}))}static get lumoInjector(){return{is:this.is,includeBaseStyles:!1}}connectedCallback(){super.connectedCallback();let e=tr(this);e.__lumoInjectorDisabled||this.isConnected&&(e.__lumoInjector||=new Ne(e),this.__lumoInjector=e.__lumoInjector,this.__lumoInjector.componentConnected(this))}disconnectedCallback(){super.disconnectedCallback(),this.__lumoInjector&&(this.__lumoInjector.componentDisconnected(this),this.__lumoInjector=void 0)}};var Pe=r=>class extends r{static get properties(){return{_theme:{type:String,readOnly:!0}}}static get observedAttributes(){return[...super.observedAttributes,"theme"]}attributeChangedCallback(e,i,o){super.attributeChangedCallback(e,i,o),e==="theme"&&this._set_theme(o)}};var $t=[],zo=new Set,jo=new Set;function Vo(r){return r&&Object.prototype.hasOwnProperty.call(r,"__themes")}function Uo(r,t){return(r||"").split(" ").some(e=>new RegExp(`^${e.split("*").join(".*")}$`,"u").test(t))}function Ho(r){return r.map(t=>t.cssText).join(`
`)}var qo="vaadin-themable-mixin-style";function Ko(r,t){let e=document.createElement("style");e.id=qo,e.textContent=Ho(r),t.content.appendChild(e)}function Wo(r=""){let t=0;return r.startsWith("lumo-")||r.startsWith("material-")?t=1:r.startsWith("vaadin-")&&(t=2),t}function ir(r){let t=[];return r.include&&[].concat(r.include).forEach(e=>{let i=$t.find(o=>o.moduleId===e);i?t.push(...ir(i),...i.styles):console.warn(`Included moduleId ${e} not found in style registry`)},r.styles),t}function Go(r){let t=`${r}-default-theme`,e=$t.filter(i=>i.moduleId!==t&&Uo(i.themeFor,r)).map(i=>({...i,styles:[...ir(i),...i.styles],includePriority:Wo(i.moduleId)})).sort((i,o)=>o.includePriority-i.includePriority);return e.length>0?e:$t.filter(i=>i.moduleId===t)}var y=r=>class extends Pe(r){constructor(){super(),zo.add(new WeakRef(this))}static finalize(){if(super.finalize(),this.is&&jo.add(this.is),this.elementStyles)return;let e=this.prototype._template;!e||Vo(this)||Ko(this.getStylesForThis(),e)}static finalizeStyles(e){return this.baseStyles=e?[e].flat(1/0):[],this.themeStyles=this.getStylesForThis(),[...this.baseStyles,...this.themeStyles]}static getStylesForThis(){let e=r.__themes||[],i=Object.getPrototypeOf(this.prototype),o=(i?i.constructor.__themes:[])||[];this.__themes=[...e,...o,...Go(this.is)];let s=this.__themes.flatMap(n=>n.styles);return s.filter((n,a)=>a===s.lastIndexOf(n))}};["--vaadin-text-color","--vaadin-text-color-disabled","--vaadin-text-color-secondary","--vaadin-border-color","--vaadin-border-color-secondary","--vaadin-background-color"].forEach(r=>{$e({name:r,syntax:"<color>",inherits:!0,initialValue:"transparent"})});Gi("vaadin-base",c`
    @layer vaadin.base {
      html {
        /* Background color */
        --vaadin-background-color: light-dark(#fff, #222);

        /* Container colors */
        --vaadin-background-container: color-mix(in oklab, var(--vaadin-text-color) 5%, var(--vaadin-background-color));
        --vaadin-background-container-strong: color-mix(
          in oklab,
          var(--vaadin-text-color) 10%,
          var(--vaadin-background-color)
        );

        /* Border colors */
        --vaadin-border-color-secondary: color-mix(in oklab, var(--vaadin-text-color) 24%, transparent);
        --vaadin-border-color: color-mix(in oklab, var(--vaadin-text-color) 48%, transparent); /* Above 3:1 contrast */

        /* Text colors */
        /* Above 3:1 contrast */
        --vaadin-text-color-disabled: color-mix(in oklab, var(--vaadin-text-color) 48%, transparent);
        /* Above 4.5:1 contrast */
        --vaadin-text-color-secondary: color-mix(in oklab, var(--vaadin-text-color) 68%, transparent);
        /* Above 7:1 contrast */
        --vaadin-text-color: light-dark(#1f1f1f, white);

        /* Padding */
        --vaadin-padding-xs: 6px;
        --vaadin-padding-s: 8px;
        --vaadin-padding-m: 12px;
        --vaadin-padding-l: 16px;
        --vaadin-padding-xl: 24px;
        --vaadin-padding-block-container: var(--vaadin-padding-xs);
        --vaadin-padding-inline-container: var(--vaadin-padding-s);

        /* Gap/spacing */
        --vaadin-gap-xs: 6px;
        --vaadin-gap-s: 8px;
        --vaadin-gap-m: 12px;
        --vaadin-gap-l: 16px;
        --vaadin-gap-xl: 24px;

        /* Border radius */
        --vaadin-radius-s: 3px;
        --vaadin-radius-m: 6px;
        --vaadin-radius-l: 12px;

        /* Focus outline */
        --vaadin-focus-ring-width: 2px;
        --vaadin-focus-ring-color: var(--vaadin-text-color);

        /* Icons, used as mask-image */
        --_vaadin-icon-arrow-up: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>');
        --_vaadin-icon-calendar: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/></svg>');
        --_vaadin-icon-checkmark: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>');
        --_vaadin-icon-chevron-down: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>');
        --_vaadin-icon-chevron-right: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>');
        --_vaadin-icon-clock: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6v6l4 2"/><circle cx="12" cy="12" r="10"/></svg>');
        --_vaadin-icon-cross: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>');
        --_vaadin-icon-drag: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"><path d="M11 7c0 .82843-.6716 1.5-1.5 1.5C8.67157 8.5 8 7.82843 8 7s.67157-1.5 1.5-1.5c.8284 0 1.5.67157 1.5 1.5Zm0 5c0 .8284-.6716 1.5-1.5 1.5-.82843 0-1.5-.6716-1.5-1.5s.67157-1.5 1.5-1.5c.8284 0 1.5.6716 1.5 1.5Zm0 5c0 .8284-.6716 1.5-1.5 1.5-.82843 0-1.5-.6716-1.5-1.5s.67157-1.5 1.5-1.5c.8284 0 1.5.6716 1.5 1.5Zm5-10c0 .82843-.6716 1.5-1.5 1.5S13 7.82843 13 7s.6716-1.5 1.5-1.5S16 6.17157 16 7Zm0 5c0 .8284-.6716 1.5-1.5 1.5S13 12.8284 13 12s.6716-1.5 1.5-1.5 1.5.6716 1.5 1.5Zm0 5c0 .8284-.6716 1.5-1.5 1.5S13 17.8284 13 17s.6716-1.5 1.5-1.5 1.5.6716 1.5 1.5Z" fill="currentColor"/></svg>');
        --_vaadin-icon-ellipsis: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>');
        --_vaadin-icon-eye: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" /><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" /></svg>');
        --_vaadin-icon-eye-slash: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88" /></svg>');
        --_vaadin-icon-file: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>');
        --_vaadin-icon-fullscreen: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" /></svg>');
        --_vaadin-icon-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>');
        --_vaadin-icon-link: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>');
        --_vaadin-icon-menu: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" /></svg>');
        --_vaadin-icon-minus: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/></svg>');
        --_vaadin-icon-paper-airplane: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 12 3.269 3.125A59.769 59.769 0 0 1 21.485 12 59.768 59.768 0 0 1 3.27 20.875L5.999 12Zm0 0h7.5" /></svg>');
        --_vaadin-icon-pen: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/></svg>');
        --_vaadin-icon-play: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z" /></svg>');
        --_vaadin-icon-plus: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>');
        --_vaadin-icon-redo: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3l3 2.7"/></svg>');
        --_vaadin-icon-refresh: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"><path d="M22 10C22 10 19.995 7.26822 18.3662 5.63824C16.7373 4.00827 14.4864 3 12 3C7.02944 3 3 7.02944 3 12C3 16.9706 7.02944 21 12 21C16.1031 21 19.5649 18.2543 20.6482 14.5M22 10V4M22 10H16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>');
        --_vaadin-icon-resize: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><path fill-rule="evenodd" clip-rule="evenodd" d="M18.5303 7.46967c.2929.29289.2929.76777 0 1.06066L8.53033 18.5304c-.29289.2929-.76777.2929-1.06066 0s-.29289-.7678 0-1.0607L17.4697 7.46967c.2929-.29289.7677-.29289 1.0606 0Zm0 4.50003c.2929.2929.2929.7678 0 1.0607l-5.5 5.5c-.2929.2928-.7677.2928-1.0606 0-.2929-.2929-.2929-.7678 0-1.0607l5.4999-5.5c.2929-.2929.7678-.2929 1.0607 0Zm0 4.5c.2929.2928.2929.7677 0 1.0606l-1 1.0001c-.2929.2928-.7677.2929-1.0606 0-.2929-.2929-.2929-.7678 0-1.0607l1-1c.2929-.2929.7677-.2929 1.0606 0Z" fill="currentColor"/></svg>');
        --_vaadin-icon-slash: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"><rect x="13.7812" y="4.22583" width="1.5" height="16" rx="0.75" transform="rotate(20 13.7812 4.22583)" fill="currentColor"/></svg>');
        --_vaadin-icon-sort: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="8" height="12" viewBox="0 0 8 12" fill="none"><path d="M7.49854 6.99951C7.92795 6.99951 8.15791 7.50528 7.87549 7.82861L4.37646 11.8296C4.17728 12.0571 3.82272 12.0571 3.62354 11.8296L0.125488 7.82861C-0.157248 7.50531 0.0719873 6.99956 0.501465 6.99951H7.49854ZM3.62354 0.17041C3.82275 -0.0573875 4.17725 -0.0573848 4.37646 0.17041L7.87549 4.17041C8.15825 4.49373 7.92806 5.00049 7.49854 5.00049L0.501465 4.99951C0.0719873 4.99946 -0.157248 4.49371 0.125488 4.17041L3.62354 0.17041Z" fill="black"/></svg>');
        --_vaadin-icon-undo: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/></svg>');
        --_vaadin-icon-upload: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m17 8-5-5-5 5"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/></svg>');
        --_vaadin-icon-user: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>');
        --_vaadin-icon-warn: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>');

        /* Cursors for interactive elements */
        --vaadin-clickable-cursor: pointer;
        --vaadin-disabled-cursor: not-allowed;

        /* Use units so that the values can be used in calc() */
        --safe-area-inset-top: env(safe-area-inset-top, 0px);
        --safe-area-inset-right: env(safe-area-inset-right, 0px);
        --safe-area-inset-bottom: env(safe-area-inset-bottom, 0px);
        --safe-area-inset-left: env(safe-area-inset-left, 0px);
        --safe-area-inset-inline-start: var(--safe-area-inset-left);
        --safe-area-inset-inline-end: var(--safe-area-inset-right);

        &:dir(rtl) {
          --safe-area-inset-inline-start: var(--safe-area-inset-right);
          --safe-area-inset-inline-end: var(--safe-area-inset-left);
        }
      }

      @supports not (color: hsl(0 0 0)) {
        html {
          --_vaadin-safari-17-deg: 1deg;
        }
      }

      @media (forced-colors: active) {
        html {
          --vaadin-background-color: Canvas;
          --vaadin-border-color: CanvasText;
          --vaadin-border-color-secondary: CanvasText;
          --vaadin-text-color-disabled: CanvasText;
          --vaadin-text-color-secondary: CanvasText;
          --vaadin-text-color: CanvasText;
          --vaadin-icon-color: CanvasText;
          --vaadin-focus-ring-color: Highlight;
        }
      }
    }
  `);var rr=c`
  :host {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: var(--vaadin-button-gap, 0 var(--vaadin-gap-s));
    white-space: var(--vaadin-button-label-wrap, normal);
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none;
    user-select: none;
    cursor: var(--vaadin-clickable-cursor);
    box-sizing: border-box;
    flex-shrink: 0;
    height: var(--vaadin-button-height, fit-content);
    margin: var(--vaadin-button-margin, 0);
    padding: var(--vaadin-button-padding, var(--vaadin-padding-block-container) var(--vaadin-padding-inline-container));
    font-family: var(--vaadin-button-font-family, inherit);
    font-size: var(--vaadin-button-font-size, inherit);
    line-height: var(--vaadin-button-line-height, inherit);
    font-weight: var(--vaadin-button-font-weight, 500);
    color: var(--vaadin-button-text-color, var(--vaadin-text-color));
    background: var(--vaadin-button-background, var(--vaadin-background-container));
    background-origin: border-box;
    border: var(--vaadin-button-border-width, 1px) solid
      var(--vaadin-button-border-color, var(--vaadin-border-color-secondary));
    border-radius: var(--vaadin-button-border-radius, var(--vaadin-radius-m));
    touch-action: manipulation;
  }

  :host([hidden]) {
    display: none !important;
  }

  .vaadin-button-container,
  [part='prefix'],
  [part='suffix'] {
    display: contents;
  }

  [part='label'] {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  :host(:is([focus-ring], :focus-visible)) {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
    outline-offset: 1px;
  }

  :host([theme~='primary']) {
    --vaadin-button-background: var(--vaadin-text-color);
    --vaadin-button-text-color: var(--vaadin-background-color);
    --vaadin-button-border-color: transparent;
  }

  :host([theme~='tertiary']) {
    background: transparent;
    border-color: transparent;
  }

  :host([disabled]) {
    pointer-events: var(--_vaadin-button-disabled-pointer-events, none);
    cursor: var(--vaadin-disabled-cursor);
    opacity: 0.5;
  }

  :host([disabled][theme~='primary']) {
    --vaadin-button-text-color: var(--vaadin-background-container-strong);
    --vaadin-button-background: var(--vaadin-text-color-disabled);
  }

  @media (forced-colors: active) {
    :host {
      --vaadin-button-border-width: 1px;
      --vaadin-button-background: ButtonFace;
      --vaadin-button-text-color: ButtonText;
    }

    :host([theme~='primary']) {
      forced-color-adjust: none;
      --vaadin-button-background: CanvasText;
      --vaadin-button-text-color: Canvas;
      --vaadin-icon-color: Canvas;
    }

    ::slotted(*) {
      forced-color-adjust: auto;
    }

    :host([disabled]) {
      --vaadin-button-background: transparent !important;
      --vaadin-button-border-color: GrayText !important;
      --vaadin-button-text-color: GrayText !important;
      opacity: 1;
    }
  }
`;var Xo=!1,Yo=r=>r,Dt=typeof document.head.style.touchAction=="string",Nt="__polymerGestures",Ot="__polymerGesturesHandled",Pt="__polymerGesturesTouchAction",or=25,sr=5,Zo=2,Jo=["mousedown","mousemove","mouseup","click"],Qo=[0,1,4,2],es=(function(){try{return new MouseEvent("test",{buttons:1}).buttons===1}catch{return!1}})();function Rt(r){return Jo.indexOf(r)>-1}var lr=!1;(function(){try{let r=Object.defineProperty({},"passive",{get(){lr=!0}});window.addEventListener("test",null,r),window.removeEventListener("test",null,r)}catch{}})();function ts(r){if(!(Rt(r)||r==="touchend")&&Dt&&lr&&Xo)return{passive:!0}}var is=navigator.userAgent.match(/iP(?:[oa]d|hone)|Android/u),rs={button:!0,command:!0,fieldset:!0,input:!0,keygen:!0,optgroup:!0,option:!0,select:!0,textarea:!0};function J(r){let t=r.type;if(!Rt(t))return!1;if(t==="mousemove"){let i=r.buttons??1;return r instanceof window.MouseEvent&&!es&&(i=Qo[r.which]||0),!!(i&1)}return(r.button??0)===0}function os(r){if(r.type==="click"){if(r.detail===0)return!0;let t=B(r);if(!t.nodeType||t.nodeType!==Node.ELEMENT_NODE)return!0;let e=t.getBoundingClientRect(),i=r.pageX,o=r.pageY;return!(i>=e.left&&i<=e.right&&o>=e.top&&o<=e.bottom)}return!1}var N={mouse:{target:null,mouseIgnoreJob:null},touch:{x:0,y:0,id:-1,scrollDecided:!1}};function ss(r){let t="auto",e=cr(r);for(let i=0,o;i<e.length;i++)if(o=e[i],o[Pt]){t=o[Pt];break}return t}function dr(r,t,e){r.movefn=t,r.upfn=e,document.addEventListener("mousemove",t),document.addEventListener("mouseup",e)}function ne(r){document.removeEventListener("mousemove",r.movefn),document.removeEventListener("mouseup",r.upfn),r.movefn=null,r.upfn=null}var cr=window.ShadyDOM&&window.ShadyDOM.noPatch?window.ShadyDOM.composedPath:r=>r.composedPath&&r.composedPath()||[],Bt={},Z=[];function ns(r,t){let e=document.elementFromPoint(r,t),i=e;for(;i?.shadowRoot&&!window.ShadyDOM;){let o=i;if(i=i.shadowRoot.elementFromPoint(r,t),o===i)break;i&&(e=i)}return e}function B(r){let t=cr(r);return t.length>0?t[0]:r.target}function as(r){let t=r.type,i=r.currentTarget[Nt];if(!i)return;let o=i[t];if(!o)return;if(!r[Ot]&&(r[Ot]={},t.startsWith("touch"))){let n=r.changedTouches[0];if(t==="touchstart"&&r.touches.length===1&&(N.touch.id=n.identifier),N.touch.id!==n.identifier)return;Dt||(t==="touchstart"||t==="touchmove")&&ls(r)}let s=r[Ot];if(!s.skip){for(let n=0,a;n<Z.length;n++)a=Z[n],o[a.name]&&!s[a.name]&&a.flow&&a.flow.start.indexOf(r.type)>-1&&a.reset&&a.reset();for(let n=0,a;n<Z.length;n++)a=Z[n],o[a.name]&&!s[a.name]&&(s[a.name]=!0,a[t](r))}}function ls(r){let t=r.changedTouches[0],e=r.type;if(e==="touchstart")N.touch.x=t.clientX,N.touch.y=t.clientY,N.touch.scrollDecided=!1;else if(e==="touchmove"){if(N.touch.scrollDecided)return;N.touch.scrollDecided=!0;let i=ss(r),o=!1,s=Math.abs(N.touch.x-t.clientX),n=Math.abs(N.touch.y-t.clientY);r.cancelable&&(i==="none"?o=!0:i==="pan-x"?o=n>s:i==="pan-y"&&(o=s>n)),o?r.preventDefault():De("track")}}function Ft(r,t,e){return Bt[t]?(ds(r,t,e),!0):!1}function ds(r,t,e){let i=Bt[t],o=i.deps,s=i.name,n=r[Nt];n||(r[Nt]=n={});for(let a=0,l,d;a<o.length;a++)l=o[a],!(is&&Rt(l)&&l!=="click")&&(d=n[l],d||(n[l]=d={_count:0}),d._count===0&&r.addEventListener(l,as,ts(l)),d[s]=(d[s]||0)+1,d._count=(d._count||0)+1);r.addEventListener(t,e),i.touchAction&&hs(r,i.touchAction)}function zt(r){Z.push(r),r.emits.forEach(t=>{Bt[t]=r})}function cs(r){for(let t=0,e;t<Z.length;t++){e=Z[t];for(let i=0,o;i<e.emits.length;i++)if(o=e.emits[i],o===r)return e}return null}function hs(r,t){Dt&&r instanceof HTMLElement&&Ri.run(()=>{r.style.touchAction=t}),r[Pt]=t}function jt(r,t,e){let i=new Event(t,{bubbles:!0,cancelable:!0,composed:!0});if(i.detail=e,Yo(r).dispatchEvent(i),i.defaultPrevented){let o=e.preventer||e.sourceEvent;o?.preventDefault&&o.preventDefault()}}function De(r){let t=cs(r);t.info&&(t.info.prevent=!0)}zt({name:"downup",deps:["mousedown","touchstart","touchend"],flow:{start:["mousedown","touchstart"],end:["mouseup","touchend"]},emits:["down","up"],info:{movefn:null,upfn:null},reset(){ne(this.info)},mousedown(r){if(!J(r))return;let t=B(r),e=this,i=s=>{J(s)||(be("up",t,s),ne(e.info))},o=s=>{J(s)&&be("up",t,s),ne(e.info)};dr(this.info,i,o),be("down",t,r)},touchstart(r){be("down",B(r),r.changedTouches[0],r)},touchend(r){be("up",B(r),r.changedTouches[0],r)}});function be(r,t,e,i){t&&jt(t,r,{x:e.clientX,y:e.clientY,sourceEvent:e,preventer:i,prevent(o){return De(o)}})}zt({name:"track",touchAction:"none",deps:["mousedown","touchstart","touchmove","touchend"],flow:{start:["mousedown","touchstart"],end:["mouseup","touchend"]},emits:["track"],info:{x:0,y:0,state:"start",started:!1,moves:[],addMove(r){this.moves.length>Zo&&this.moves.shift(),this.moves.push(r)},movefn:null,upfn:null,prevent:!1},reset(){this.info.state="start",this.info.started=!1,this.info.moves=[],this.info.x=0,this.info.y=0,this.info.prevent=!1,ne(this.info)},mousedown(r){if(!J(r))return;let t=B(r),e=this,i=s=>{let n=s.clientX,a=s.clientY;nr(e.info,n,a)&&(e.info.state=e.info.started?s.type==="mouseup"?"end":"track":"start",e.info.state==="start"&&De("tap"),e.info.addMove({x:n,y:a}),J(s)||(e.info.state="end",ne(e.info)),t&&Lt(e.info,t,s),e.info.started=!0)},o=s=>{e.info.started&&i(s),ne(e.info)};dr(this.info,i,o),this.info.x=r.clientX,this.info.y=r.clientY},touchstart(r){let t=r.changedTouches[0];this.info.x=t.clientX,this.info.y=t.clientY},touchmove(r){let t=B(r),e=r.changedTouches[0],i=e.clientX,o=e.clientY;nr(this.info,i,o)&&(this.info.state==="start"&&De("tap"),this.info.addMove({x:i,y:o}),Lt(this.info,t,e),this.info.state="track",this.info.started=!0)},touchend(r){let t=B(r),e=r.changedTouches[0];this.info.started&&(this.info.state="end",this.info.addMove({x:e.clientX,y:e.clientY}),Lt(this.info,t,e))}});function nr(r,t,e){if(r.prevent)return!1;if(r.started)return!0;let i=Math.abs(r.x-t),o=Math.abs(r.y-e);return i>=sr||o>=sr}function Lt(r,t,e){if(!t)return;let i=r.moves[r.moves.length-2],o=r.moves[r.moves.length-1],s=o.x-r.x,n=o.y-r.y,a,l=0;i&&(a=o.x-i.x,l=o.y-i.y),jt(t,"track",{state:r.state,x:e.clientX,y:e.clientY,dx:s,dy:n,ddx:a,ddy:l,sourceEvent:e,hover(){return ns(e.clientX,e.clientY)}})}zt({name:"tap",deps:["mousedown","click","touchstart","touchend"],flow:{start:["mousedown","touchstart"],end:["click","touchend"]},emits:["tap"],info:{x:NaN,y:NaN,prevent:!1},reset(){this.info.x=NaN,this.info.y=NaN,this.info.prevent=!1},mousedown(r){J(r)&&(this.info.x=r.clientX,this.info.y=r.clientY)},click(r){J(r)&&ar(this.info,r)},touchstart(r){let t=r.changedTouches[0];this.info.x=t.clientX,this.info.y=t.clientY},touchend(r){ar(this.info,r.changedTouches[0],r)}});function ar(r,t,e){let i=Math.abs(t.clientX-r.x),o=Math.abs(t.clientY-r.y),s=B(e||t);!s||rs[s.localName]&&s.hasAttribute("disabled")||(isNaN(i)||isNaN(o)||i<=or&&o<=or||os(t))&&(r.prevent||jt(s,"tap",{x:t.clientX,y:t.clientY,sourceEvent:t,preventer:e}))}var us=r=>class extends r{static get properties(){return{disabled:{type:Boolean,value:!1,observer:"_disabledChanged",reflectToAttribute:!0,sync:!0}}}_disabledChanged(e){this._setAriaDisabled(e)}_setAriaDisabled(e){e?this.setAttribute("aria-disabled","true"):this.removeAttribute("aria-disabled")}click(){this.disabled||super.click()}},F=x(us);var ps=r=>class extends r{ready(){super.ready(),this.addEventListener("keydown",e=>{this._onKeyDown(e)}),this.addEventListener("keyup",e=>{this._onKeyUp(e)})}_onKeyDown(e){switch(e.key){case"Enter":this._onEnter(e);break;case"Escape":this._onEscape(e);break;default:break}}_onKeyUp(e){}_onEnter(e){}_onEscape(e){}},z=x(ps);var j=r=>class extends F(z(r)){get _activeKeys(){return[" "]}ready(){super.ready(),Ft(this,"down",e=>{this._shouldSetActive(e)&&this._setActive(!0)}),Ft(this,"up",()=>{this._setActive(!1)})}disconnectedCallback(){super.disconnectedCallback(),this._setActive(!1)}_shouldSetActive(e){return!this.disabled}_onKeyDown(e){super._onKeyDown(e),this._shouldSetActive(e)&&this._activeKeys.includes(e.key)&&(this._setActive(!0),document.addEventListener("keyup",i=>{this._activeKeys.includes(i.key)&&this._setActive(!1)},{once:!0}))}_setActive(e){this.toggleAttribute("active",e)}};var Ut=!1;window.addEventListener("keydown",()=>{Ut=!0},{capture:!0});window.addEventListener("mousedown",()=>{Ut=!1},{capture:!0});function ye(){let r=document.activeElement||document.body;for(;r.shadowRoot&&r.shadowRoot.activeElement;)r=r.shadowRoot.activeElement;return r}function V(){return Ut}function hr(r){let t=r.style;if(t.visibility==="hidden"||t.display==="none")return!0;let e=window.getComputedStyle(r);return e.visibility==="hidden"||e.display==="none"}function fs(r,t){let e=Math.max(r.tabIndex,0),i=Math.max(t.tabIndex,0);return e===0||i===0?i>e:e>i}function ms(r,t){let e=[];for(;r.length>0&&t.length>0;)fs(r[0],t[0])?e.push(t.shift()):e.push(r.shift());return e.concat(r,t)}function Vt(r){let t=r.length;if(t<2)return r;let e=Math.ceil(t/2),i=Vt(r.slice(0,e)),o=Vt(r.slice(e));return ms(i,o)}function Q(r){return r.checkVisibility?!r.checkVisibility({visibilityProperty:!0}):r.offsetParent===null&&r.clientWidth===0&&r.clientHeight===0?!0:hr(r)}function vs(r){return r.matches('[tabindex="-1"]')?!1:r.matches("input, select, textarea, button, object")?r.matches(":not([disabled])"):r.matches("a[href], area[href], iframe, [tabindex], [contentEditable]")}function Re(r){return r.getRootNode().activeElement===r}function _s(r){if(!vs(r))return-1;let t=r.getAttribute("tabindex")||0;return Number(t)}function ur(r,t){if(r.nodeType!==Node.ELEMENT_NODE||hr(r))return!1;let e=r,i=_s(e),o=i>0;i>=0&&t.push(e);let s=[];return e.localName==="slot"?s=e.assignedNodes({flatten:!0}):s=(e.shadowRoot||e).children,[...s].forEach(n=>{o=ur(n,t)||o}),o}function pr(r){let t=[];return ur(r,t)?Vt(t):t}var gs=r=>class extends r{get _keyboardActive(){return V()}ready(){this.addEventListener("focusin",e=>{this._shouldSetFocus(e)&&this._setFocused(!0)}),this.addEventListener("focusout",e=>{this._shouldRemoveFocus(e)&&this._setFocused(!1)}),super.ready()}disconnectedCallback(){super.disconnectedCallback(),this.hasAttribute("focused")&&this._setFocused(!1)}focus(e){super.focus(e),e?.focusVisible!==!1&&this.setAttribute("focus-ring","")}_setFocused(e){this.toggleAttribute("focused",e),this.toggleAttribute("focus-ring",e&&this._keyboardActive)}_shouldSetFocus(e){return!0}_shouldRemoveFocus(e){return!0}},U=x(gs);var Be=r=>class extends F(r){static get properties(){return{tabindex:{type:Number,reflectToAttribute:!0,observer:"_tabindexChanged",sync:!0},_lastTabIndex:{type:Number}}}_disabledChanged(e,i){super._disabledChanged(e,i),!this.__shouldAllowFocusWhenDisabled()&&(e?(this.tabindex!==void 0&&(this._lastTabIndex=this.tabindex),this.setAttribute("tabindex","-1")):i&&(this._lastTabIndex!==void 0?this.setAttribute("tabindex",this._lastTabIndex):this.tabindex=void 0))}_tabindexChanged(e){this.__shouldAllowFocusWhenDisabled()||this.disabled&&e!==-1&&(this._lastTabIndex=e,this.setAttribute("tabindex","-1"))}focus(e){(!this.disabled||this.__shouldAllowFocusWhenDisabled())&&super.focus(e)}__shouldAllowFocusWhenDisabled(){return!1}};var bs=["mousedown","mouseup","click","dblclick","keypress","keydown","keyup"],Fe=r=>class extends j(Be(U(r))){constructor(){super(),this.__onInteractionEvent=this.__onInteractionEvent.bind(this),bs.forEach(e=>{this.addEventListener(e,this.__onInteractionEvent,!0)}),this.tabindex=0}get _activeKeys(){return["Enter"," "]}ready(){super.ready(),this.hasAttribute("role")||this.setAttribute("role","button"),this.__shouldAllowFocusWhenDisabled()&&this.style.setProperty("--_vaadin-button-disabled-pointer-events","auto")}_onKeyDown(e){super._onKeyDown(e),!(e.altKey||e.shiftKey||e.ctrlKey||e.metaKey)&&this._activeKeys.includes(e.key)&&(e.preventDefault(),this.click())}__onInteractionEvent(e){this.__shouldSuppressInteractionEvent(e)&&e.stopImmediatePropagation()}__shouldSuppressInteractionEvent(e){return this.disabled}};var Ht=class extends Fe(E(y(_(b(p))))){static get is(){return"vaadin-button"}static get styles(){return rr}static get properties(){return{disabled:{type:Boolean,value:!1,observer:"_disabledChanged",reflectToAttribute:!0,sync:!0}}}render(){return m`
      <div class="vaadin-button-container">
        <span part="prefix" aria-hidden="true">
          <slot name="prefix"></slot>
        </span>
        <span part="label">
          <slot></slot>
        </span>
        <span part="suffix" aria-hidden="true">
          <slot name="suffix"></slot>
        </span>

        <slot name="tooltip"></slot>
      </div>
    `}ready(){super.ready(),this._tooltipController=new I(this),this.addController(this._tooltipController)}__shouldAllowFocusWhenDisabled(){return window.Vaadin.featureFlags.accessibleDisabledButtons}};v(Ht);var ze=(r,t=r)=>c`
  :host {
    align-items: baseline;
    column-gap: var(--vaadin-${u(t)}-gap, var(--vaadin-gap-s));
    grid-template: none;
    grid-template-columns: auto 1fr;
    grid-template-rows: repeat(auto-fill, minmax(0, max-content));
    -webkit-tap-highlight-color: transparent;
    --_cursor: var(--vaadin-clickable-cursor);
  }

  :host([disabled]) {
    --_cursor: var(--vaadin-disabled-cursor);
  }

  :host(:not([has-label])) {
    column-gap: 0;
  }

  [part='${u(r)}'],
  ::slotted(input),
  [part='label'],
  ::slotted(label) {
    grid-row: 1;
  }

  [part='label'],
  ::slotted(label) {
    font-size: var(--vaadin-${u(t)}-label-font-size, var(--vaadin-input-field-label-font-size, inherit));
    line-height: var(--vaadin-${u(t)}-label-line-height, var(--vaadin-input-field-label-line-height, inherit));
    font-weight: var(--vaadin-${u(t)}-label-font-weight, var(--vaadin-input-field-label-font-weight, 500));
    color: var(--vaadin-${u(t)}-label-color, var(--vaadin-input-field-label-color, var(--vaadin-text-color)));
    word-break: break-word;
    cursor: var(--_cursor);
  }

  [part='${u(r)}'],
  ::slotted(input) {
    grid-column: 1;
  }

  [part='label'],
  [part='helper-text'],
  [part='error-message'] {
    margin-bottom: 0;
    grid-column: 2;
    width: auto;
    min-width: auto;
  }

  [part='helper-text'],
  [part='error-message'] {
    margin-top: var(--_gap-s);
    grid-row: auto;
  }

  /* Baseline vertical alignment */
  :host::before {
    grid-row: 1;
    margin: 0;
    padding: 0;
    border: 0;
  }

  /* visually hidden */
  ::slotted(input) {
    cursor: inherit;
    align-self: stretch;
    appearance: none;
    cursor: var(--_cursor);
    /* Ensure minimum click target (WCAG) */
    margin: min(0px, (24px - 100%) / -2) !important;
    /* Extend the input to cover the gap between the checkbox/radio and label */
    margin-inline-end: calc(min(0px, (24px - 100%) / -2) - var(--vaadin-${u(t)}-gap, var(--vaadin-gap-s))) !important;
  }

  /* Control container (checkbox, radio button) */
  [part='${u(r)}'] {
    background: var(--vaadin-${u(t)}-background, var(--vaadin-background-color));
    border-color: var(--vaadin-${u(t)}-border-color, var(--vaadin-input-field-border-color, var(--vaadin-border-color)));
    border-radius: var(--vaadin-${u(t)}-border-radius, var(--vaadin-radius-s));
    border-style: var(--_border-style, solid);
    --_border-width: var(--vaadin-${u(t)}-border-width, var(--vaadin-input-field-border-width, 1px));
    border-width: var(--_border-width);
    box-sizing: border-box;
    --_color: var(--vaadin-${u(t)}-marker-color, var(--vaadin-${u(t)}-background, var(--vaadin-background-color)));
    color: var(--_color);
    height: var(--vaadin-${u(t)}-size, 1lh);
    width: var(--vaadin-${u(t)}-size, 1lh);
    position: relative;
    cursor: var(--_cursor);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  :host(:is([checked], [indeterminate])) {
    --vaadin-${u(t)}-background: var(--vaadin-text-color);
    --vaadin-${u(t)}-border-color: transparent;
  }

  :host([disabled]) {
    --vaadin-${u(t)}-background: var(--vaadin-input-field-disabled-background, var(--vaadin-background-container-strong));
    --vaadin-${u(t)}-border-color: transparent;
    --vaadin-${u(t)}-marker-color: var(--vaadin-text-color-disabled);
  }

  /* Focus ring */
  :host([focus-ring]) [part='${u(r)}'] {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
    outline-offset: calc(var(--_border-width) * -1);
  }

  :host([focus-ring]:is([checked], [indeterminate])) [part='${u(r)}'] {
    outline-offset: 1px;
  }

  :host([readonly][focus-ring]) [part='${u(r)}'] {
    --vaadin-${u(t)}-border-color: transparent;
    outline-offset: calc(var(--_border-width) * -1);
    outline-style: dashed;
  }

  /* Checked indicator (checkmark, dot) */
  [part='${u(r)}']::after {
    content: '\\2003' / '';
    background: currentColor;
    border-radius: inherit;
    display: flex;
    align-items: center;
    --_filter: var(--vaadin-${u(t)}-marker-color, saturate(0) invert(1) hue-rotate(180deg) contrast(100) brightness(100));
    filter: var(--_filter);
  }

  :host(:not([checked], [indeterminate])) [part='${u(r)}']::after {
    opacity: 0;
  }

  @media (forced-colors: active) {
    :host(:is([checked], [indeterminate])) {
      --vaadin-${u(t)}-border-color: CanvasText !important;
    }

    :host(:is([checked], [indeterminate])) [part='${u(r)}'] {
      background: SelectedItem !important;
    }

    :host(:is([checked], [indeterminate])) [part='${u(r)}']::after {
      background: SelectedItemText !important;
    }

    :host([readonly]) [part='${u(r)}']::after {
      background: CanvasText !important;
    }

    :host([disabled]) {
      --vaadin-${u(t)}-border-color: GrayText !important;
    }

    :host([disabled]) [part='${u(r)}']::after {
      background: GrayText !important;
    }
  }
`;var H=c`
  :host {
    --_helper-below-field: initial;
    --_helper-above-field: ;
    --_no-label: initial;
    --_has-label: ;
    --_no-helper: initial;
    --_has-helper: ;
    --_no-error: initial;
    --_has-error: ;
    --_gap: var(--vaadin-input-field-container-gap, var(--vaadin-gap-xs));
    --_gap-s: round(var(--_gap) / 3, 2px);
    display: inline-grid;
    grid-template:
      'label' auto var(--_helper-above-field, 'helper' auto) 'baseline' 0 'input' 1fr var(
        --_helper-below-field,
        'helper' auto
      )
      'error' auto / 100%;
    height: fit-content;
    outline: none;
    cursor: default;
    -webkit-tap-highlight-color: transparent;
  }

  :host([has-label]) {
    --_has-label: initial;
    --_no-label: ;
  }

  :host([has-helper]) {
    --_has-helper: initial;
    --_no-helper: ;
  }

  :host([has-error-message]) {
    --_has-error: initial;
    --_no-error: ;
  }

  :host([hidden]) {
    display: none !important;
  }

  :host(:not([has-label])) [part='label'],
  :host(:not([has-helper])) [part='helper-text'],
  :host(:not([has-error-message])) [part='error-message'] {
    display: none;
  }

  /* Baseline alignment guide */
  :host::before {
    content: '\\2003' / '';
    grid-column: 1;
    grid-row: var(--_has-label, label / baseline) var(--_no-label, label / input);
    align-self: var(--_has-label, end) var(--_no-label, start);
    font-size: var(--vaadin-input-field-value-font-size, inherit);
    line-height: var(--vaadin-input-field-value-line-height, inherit);
    padding: var(
      --vaadin-input-field-padding,
      var(--vaadin-padding-block-container) var(--vaadin-padding-inline-container)
    );
    border: var(--vaadin-input-field-border-width, 1px) solid transparent;
    pointer-events: none;
    margin-bottom: var(--_no-label, 0)
      var(
        --_has-label,
        calc(
          var(
              --vaadin-field-baseline-input-height,
              (1lh + var(--vaadin-padding-block-container) * 2 + var(--vaadin-input-field-border-width, 1px) * 2)
            ) *
            -1
        )
      );
  }

  [class$='container'] {
    display: contents;
  }

  [part] {
    grid-column: 1;
  }

  [part='label'] {
    font-size: var(--vaadin-input-field-label-font-size, inherit);
    line-height: var(--vaadin-input-field-label-line-height, inherit);
    font-weight: var(--vaadin-input-field-label-font-weight, 500);
    color: var(--vaadin-input-field-label-color, var(--vaadin-text-color));
    word-break: break-word;
    position: relative;
    grid-area: label;
    margin-bottom: var(--_helper-below-field, var(--_gap)) var(--_helper-above-field, var(--_no-helper, var(--_gap)));
  }

  ::slotted(label) {
    cursor: inherit;
  }

  :host([disabled]) [part='label'],
  :host([disabled]) ::slotted(label) {
    opacity: 0.5;
  }

  :host([disabled]) [part='label'] ::slotted(label) {
    opacity: 1;
  }

  :host([required]) [part='label'] {
    padding-inline-end: 1em;
  }

  [part='required-indicator'] {
    display: inline-block;
    position: absolute;
    width: 1em;
    text-align: center;
    color: var(--vaadin-input-field-required-indicator-color, var(--vaadin-text-color-secondary));
  }

  [part='required-indicator']::after {
    content: var(--vaadin-input-field-required-indicator, '*');
  }

  :host(:not([required])) [part='required-indicator'] {
    display: none;
  }

  [part='label'],
  [part='helper-text'],
  [part='error-message'] {
    width: min-content;
    min-width: 100%;
    box-sizing: border-box;
  }

  [part='input-field'],
  [part='group-field'],
  [part='input-fields'] {
    grid-area: input;
  }

  [part='input-field'] {
    width: var(--vaadin-field-default-width, 12em);
    max-width: 100%;
    min-width: 100%;
  }

  :host([readonly]) [part='input-field'] {
    cursor: default;
  }

  :host([disabled]) [part='input-field'] {
    cursor: var(--vaadin-disabled-cursor);
  }

  [part='helper-text'] {
    font-size: var(--vaadin-input-field-helper-font-size, inherit);
    line-height: var(--vaadin-input-field-helper-line-height, inherit);
    font-weight: var(--vaadin-input-field-helper-font-weight, 400);
    color: var(--vaadin-input-field-helper-color, var(--vaadin-text-color-secondary));
    grid-area: helper;
    margin-top: var(--_helper-above-field, var(--_gap-s)) var(--_helper-below-field, var(--_gap));
    margin-bottom: var(--_helper-above-field, var(--_gap));
  }

  [part='error-message'] {
    font-size: var(--vaadin-input-field-error-font-size, inherit);
    line-height: var(--vaadin-input-field-error-line-height, inherit);
    font-weight: var(--vaadin-input-field-error-font-weight, 400);
    color: var(--vaadin-input-field-error-color, var(--vaadin-text-color));
    display: flex;
    gap: var(--vaadin-gap-xs);
    grid-area: error;
    margin-top: var(--_has-helper, var(--_helper-below-field, var(--_gap-s)) var(--_helper-above-field, var(--_gap)))
      var(--_no-helper, var(--_gap));
  }

  [part='error-message']::before {
    content: '';
    display: inline-block;
    flex: none;
    width: var(--vaadin-icon-size, 1lh);
    height: var(--vaadin-icon-size, 1lh);
    mask: var(--_vaadin-icon-warn) 50% / var(--vaadin-icon-visual-size, 100%) no-repeat;
    background: currentColor;
  }

  :host([theme~='helper-above-field']) {
    --_helper-above-field: initial;
    --_helper-below-field: ;
  }

  @media (forced-colors: active) {
    [part='error-message']::before {
      background: CanvasText;
    }
  }
`;var ys=c`
  [part='checkbox'] {
    color: var(--vaadin-checkbox-checkmark-color, var(--_color));
  }

  [part='checkbox']::after {
    inset: 0;
    mask: var(--_vaadin-icon-checkmark) 50% /
      var(--vaadin-checkbox-checkmark-size, var(--vaadin-checkbox-marker-size, 100%)) no-repeat;
    filter: var(--vaadin-checkbox-checkmark-color, var(--_filter));
  }

  :host([readonly]) {
    --vaadin-checkbox-background: transparent;
    --vaadin-checkbox-border-color: var(--vaadin-border-color);
    --vaadin-checkbox-marker-color: var(--vaadin-text-color);
    --_border-style: dashed;
  }

  :host([indeterminate]) [part='checkbox']::after {
    mask-image: var(--_vaadin-icon-minus);
  }
`,fr=[H,ze("checkbox"),ys];var xs=r=>class extends U(Be(r)){static get properties(){return{autofocus:{type:Boolean},focusElement:{type:Object,readOnly:!0,observer:"_focusElementChanged",sync:!0},_lastTabIndex:{value:0}}}constructor(){super(),this._boundOnBlur=this._onBlur.bind(this),this._boundOnFocus=this._onFocus.bind(this)}ready(){super.ready(),this.autofocus&&!this.disabled&&requestAnimationFrame(()=>{this.focus()})}focus(e){this.focusElement&&!this.disabled&&(this.focusElement.focus(),e?.focusVisible!==!1&&this.setAttribute("focus-ring",""))}blur(){this.focusElement&&this.focusElement.blur()}click(){this.focusElement&&!this.disabled&&this.focusElement.click()}_focusElementChanged(e,i){e?(e.disabled=this.disabled,this._addFocusListeners(e),this.__forwardTabIndex(this.tabindex)):i&&this._removeFocusListeners(i)}_addFocusListeners(e){e.addEventListener("blur",this._boundOnBlur),e.addEventListener("focus",this._boundOnFocus)}_removeFocusListeners(e){e.removeEventListener("blur",this._boundOnBlur),e.removeEventListener("focus",this._boundOnFocus)}_onFocus(e){e.stopPropagation(),this.dispatchEvent(new Event("focus"))}_onBlur(e){e.stopPropagation(),this.dispatchEvent(new Event("blur"))}_shouldSetFocus(e){return e.target===this.focusElement}_shouldRemoveFocus(e){return e.target===this.focusElement}_disabledChanged(e,i){super._disabledChanged(e,i),this.focusElement&&(this.focusElement.disabled=e),e&&this.blur()}_tabindexChanged(e){this.__forwardTabIndex(e)}__forwardTabIndex(e){e!==void 0&&this.focusElement&&(this.focusElement.tabIndex=e,e!==-1&&(this.tabindex=void 0)),this.disabled&&e&&(e!==-1&&(this._lastTabIndex=e),this.tabindex=void 0),e===void 0&&this.hasAttribute("tabindex")&&this.removeAttribute("tabindex")}},ae=x(xs);var qt=new WeakMap;function ws(r){return qt.has(r)||qt.set(r,new Set),qt.get(r)}function Cs(r,t){let e=document.createElement("style");e.textContent=r,t===document?document.head.appendChild(e):t.insertBefore(e,t.firstChild)}var As=r=>class extends r{get slotStyles(){return[]}connectedCallback(){super.connectedCallback(),this.__applySlotStyles()}__applySlotStyles(){let e=this.getRootNode(),i=ws(e);this.slotStyles.forEach(o=>{i.has(o)||(Cs(o,e),i.add(o))})}},je=x(As);var ks=r=>class extends r{static get properties(){return{stateTarget:{type:Object,observer:"_stateTargetChanged"}}}static get delegateAttrs(){return[]}static get delegateProps(){return[]}ready(){super.ready(),this._createDelegateAttrsObserver(),this._createDelegatePropsObserver()}_stateTargetChanged(e){e&&(this._ensureAttrsDelegated(),this._ensurePropsDelegated())}_createDelegateAttrsObserver(){this._createMethodObserver(`_delegateAttrsChanged(${this.constructor.delegateAttrs.join(", ")})`)}_createDelegatePropsObserver(){this._createMethodObserver(`_delegatePropsChanged(${this.constructor.delegateProps.join(", ")})`)}_ensureAttrsDelegated(){this.constructor.delegateAttrs.forEach(e=>{this._delegateAttribute(e,this[e])})}_ensurePropsDelegated(){this.constructor.delegateProps.forEach(e=>{this._delegateProperty(e,this[e])})}_delegateAttrsChanged(...e){this.constructor.delegateAttrs.forEach((i,o)=>{this._delegateAttribute(i,e[o])})}_delegatePropsChanged(...e){this.constructor.delegateProps.forEach((i,o)=>{this._delegateProperty(i,e[o])})}_delegateAttribute(e,i){this.stateTarget&&(e==="invalid"&&this._delegateAttribute("aria-invalid",i?"true":!1),typeof i=="boolean"?this.stateTarget.toggleAttribute(e,i):i?this.stateTarget.setAttribute(e,i):this.stateTarget.removeAttribute(e))}_delegateProperty(e,i){this.stateTarget&&(this.stateTarget[e]=i)}},Ve=x(ks);var Es=r=>class extends r{static get properties(){return{inputElement:{type:Object,readOnly:!0,observer:"_inputElementChanged",sync:!0},type:{type:String,readOnly:!0},value:{type:String,value:"",observer:"_valueChanged",notify:!0,sync:!0}}}constructor(){super(),this._boundOnInput=this._onInput.bind(this),this._boundOnChange=this._onChange.bind(this)}get _hasValue(){return this.value!=null&&this.value!==""}get _inputElementValueProperty(){return"value"}get _inputElementValue(){return this.inputElement?this.inputElement[this._inputElementValueProperty]:void 0}set _inputElementValue(e){this.inputElement&&(this.inputElement[this._inputElementValueProperty]=e)}clear(){this.value="",this._inputElementValue=""}_addInputListeners(e){e.addEventListener("input",this._boundOnInput),e.addEventListener("change",this._boundOnChange)}_removeInputListeners(e){e.removeEventListener("input",this._boundOnInput),e.removeEventListener("change",this._boundOnChange)}_forwardInputValue(e){this.inputElement&&(this._inputElementValue=e??"")}_inputElementChanged(e,i){e?this._addInputListeners(e):i&&this._removeInputListeners(i)}_onInput(e){let i=e.composedPath()[0];this.__userInput=e.isTrusted,this.value=i.value,this.__userInput=!1}_onChange(e){}_toggleHasValue(e){this.toggleAttribute("has-value",e)}_valueChanged(e,i){this._toggleHasValue(this._hasValue),!(e===""&&i===void 0)&&(this.__userInput||this._forwardInputValue(e))}},mr=x(Es);var Ue=r=>class extends Ve(F(mr(r))){static get properties(){return{checked:{type:Boolean,value:!1,notify:!0,reflectToAttribute:!0,sync:!0}}}static get delegateProps(){return[...super.delegateProps,"checked"]}_onChange(e){let i=e.target;this._toggleChecked(i.checked)}_toggleChecked(e){this.checked=e}};var Kt=new Map;function Wt(r){return Kt.has(r)||Kt.set(r,new WeakMap),Kt.get(r)}function vr(r,t){r&&r.removeAttribute(t)}function _r(r,t){if(!r||!t)return;let e=Wt(t);if(e.has(r))return;let i=Ie(r.getAttribute(t));e.set(r,new Set(i))}function gr(r,t){if(!r||!t)return;let e=Wt(t),i=e.get(r);!i||i.size===0?r.removeAttribute(t):Et(r,t,ge(i)),e.delete(r)}function q(r,t,e={newId:null,oldId:null,fromUser:!1}){if(!r||!t)return;let{newId:i,oldId:o,fromUser:s}=e,n=Wt(t),a=n.get(r);if(!s&&a){o&&a.delete(o),i&&a.add(i);return}s&&(a?i||n.delete(r):_r(r,t),vr(r,t)),Ki(r,t,o);let l=i||ge(a);l&&Et(r,t,l)}function br(r,t){_r(r,t),vr(r,t)}var He=class{constructor(t){this.host=t,this.__required=!1}setTarget(t){this.__target=t,this.__setAriaRequiredAttribute(this.__required),this.__setLabelIdToAriaAttribute(this.__labelId,this.__labelId),this.__labelIdFromUser!=null&&this.__setLabelIdToAriaAttribute(this.__labelIdFromUser,this.__labelIdFromUser,!0),this.__setErrorIdToAriaAttribute(this.__errorId),this.__setHelperIdToAriaAttribute(this.__helperId),this.setAriaLabel(this.__label)}setRequired(t){this.__setAriaRequiredAttribute(t),this.__required=t}setAriaLabel(t){this.__setAriaLabelToAttribute(t),this.__label=t}setLabelId(t,e=!1){let i=e?this.__labelIdFromUser:this.__labelId;this.__setLabelIdToAriaAttribute(t,i,e),e?this.__labelIdFromUser=t:this.__labelId=t}setErrorId(t){this.__setErrorIdToAriaAttribute(t,this.__errorId),this.__errorId=t}setHelperId(t){this.__setHelperIdToAriaAttribute(t,this.__helperId),this.__helperId=t}__setAriaLabelToAttribute(t){this.__target&&(t?(br(this.__target,"aria-labelledby"),this.__target.setAttribute("aria-label",t)):this.__label&&(gr(this.__target,"aria-labelledby"),this.__target.removeAttribute("aria-label")))}__setLabelIdToAriaAttribute(t,e,i){q(this.__target,"aria-labelledby",{newId:t,oldId:e,fromUser:i})}__setErrorIdToAriaAttribute(t,e){q(this.__target,"aria-describedby",{newId:t,oldId:e,fromUser:!1})}__setHelperIdToAriaAttribute(t,e){q(this.__target,"aria-describedby",{newId:t,oldId:e,fromUser:!1})}__setAriaRequiredAttribute(t){this.__target&&(["input","textarea"].includes(this.__target.localName)||(t?this.__target.setAttribute("aria-required","true"):this.__target.removeAttribute("aria-required")))}};var M=document.createElement("div");M.style.position="fixed";M.style.clip="rect(0px, 0px, 0px, 0px)";M.setAttribute("aria-live","polite");document.body.appendChild(M);var qe;function yr(r,t={}){let e=t.mode||"polite",i=t.timeout??150;e==="alert"?(M.removeAttribute("aria-live"),M.removeAttribute("role"),qe=D.debounce(qe,Pi,()=>{M.setAttribute("role","alert")})):(qe&&qe.cancel(),M.removeAttribute("role"),M.setAttribute("aria-live",e)),M.textContent="",setTimeout(()=>{M.textContent=r},i)}var K=class extends A{constructor(t,e,i,o={}){super(t,e,i,{...o,useUniqueId:!0})}initCustomNode(t){this.__updateNodeId(t),this.__notifyChange(t)}teardownNode(t){let e=this.getSlotChild();e&&e!==this.defaultNode?this.__notifyChange(e):(this.restoreDefaultNode(),this.updateDefaultNode(this.node))}attachDefaultNode(){let t=super.attachDefaultNode();return t&&this.__updateNodeId(t),t}restoreDefaultNode(){}updateDefaultNode(t){this.__notifyChange(t)}observeNode(t){this.__nodeObserver&&this.__nodeObserver.disconnect(),this.__nodeObserver=new MutationObserver(e=>{e.forEach(i=>{let o=i.target,s=o===this.node;i.type==="attributes"?s&&this.__updateNodeId(o):(s||o.parentElement===this.node)&&this.__notifyChange(this.node)})}),this.__nodeObserver.observe(t,{attributes:!0,attributeFilter:["id"],childList:!0,subtree:!0,characterData:!0})}__hasContent(t){return t?t.nodeType===Node.ELEMENT_NODE&&(customElements.get(t.localName)||t.children.length>0)||t.textContent&&t.textContent.trim()!=="":!1}__notifyChange(t){this.dispatchEvent(new CustomEvent("slot-content-changed",{detail:{hasContent:this.__hasContent(t),node:t}}))}__updateNodeId(t){let e=!this.nodes||t===this.nodes[0];t.nodeType===Node.ELEMENT_NODE&&(!this.multiple||e)&&!t.id&&(t.id=this.defaultId)}};var Ke=class extends K{constructor(t){super(t,"error-message","div")}setErrorMessage(t){this.errorMessage=t,this.updateDefaultNode(this.node)}setInvalid(t){this.invalid=t,this.updateDefaultNode(this.node)}initAddedNode(t){t!==this.defaultNode&&this.initCustomNode(t)}initNode(t){this.updateDefaultNode(t)}initCustomNode(t){t.textContent&&!this.errorMessage&&(this.errorMessage=t.textContent.trim()),super.initCustomNode(t)}restoreDefaultNode(){this.attachDefaultNode()}updateDefaultNode(t){let{errorMessage:e,invalid:i}=this,o=!!(i&&e&&e.trim()!=="");t&&(t.textContent=o?e:"",t.hidden=!o,o&&yr(e,{mode:"assertive"})),super.updateDefaultNode(t)}};var We=class extends K{constructor(t){super(t,"helper",null)}setHelperText(t){this.helperText=t,this.getSlotChild()||this.restoreDefaultNode(),this.node===this.defaultNode&&this.updateDefaultNode(this.node)}restoreDefaultNode(){let{helperText:t}=this;if(t&&t.trim()!==""){this.tagName="div";let e=this.attachDefaultNode();this.observeNode(e)}}updateDefaultNode(t){t&&(t.textContent=this.helperText),super.updateDefaultNode(t)}initCustomNode(t){super.initCustomNode(t),this.observeNode(t)}};var le=class extends K{constructor(t){super(t,"label","label")}setLabel(t){this.label=t,this.getSlotChild()||this.restoreDefaultNode(),this.node===this.defaultNode&&this.updateDefaultNode(this.node)}restoreDefaultNode(){let{label:t}=this;if(t&&t.trim()!==""){let e=this.attachDefaultNode();this.observeNode(e)}}updateDefaultNode(t){t&&(t.textContent=this.label),super.updateDefaultNode(t)}initCustomNode(t){super.initCustomNode(t),this.observeNode(t)}};var Ge=r=>class extends r{static get properties(){return{label:{type:String,observer:"_labelChanged"}}}constructor(){super(),this._labelController=new le(this),this._labelController.addEventListener("slot-content-changed",e=>{this.toggleAttribute("has-label",e.detail.hasContent)})}get _labelId(){return this._labelNode?.id}get _labelNode(){return this._labelController.node}ready(){super.ready(),this.addController(this._labelController)}_labelChanged(e){this._labelController.setLabel(e)}};var Ss=r=>class extends r{static get properties(){return{invalid:{type:Boolean,reflectToAttribute:!0,notify:!0,value:!1,sync:!0},manualValidation:{type:Boolean,value:!1},required:{type:Boolean,reflectToAttribute:!0,sync:!0}}}validate(){let t=this.checkValidity();return this._setInvalid(!t),this.dispatchEvent(new CustomEvent("validated",{detail:{valid:t}})),t}checkValidity(){return!this.required||!!this.value}_setInvalid(t){this._shouldSetInvalid(t)&&(this.invalid=t)}_shouldSetInvalid(t){return!0}_requestValidation(){this.manualValidation||this.validate()}},xr=x(Ss);var de=r=>class extends xr(Ge(r)){static get properties(){return{ariaTarget:{type:Object,observer:"_ariaTargetChanged"},errorMessage:{type:String,observer:"_errorMessageChanged"},helperText:{type:String,observer:"_helperTextChanged"},accessibleName:{type:String,observer:"_accessibleNameChanged"},accessibleNameRef:{type:String,observer:"_accessibleNameRefChanged"}}}static get observers(){return["_invalidChanged(invalid)","_requiredChanged(required)"]}constructor(){super(),this._fieldAriaController=new He(this),this._helperController=new We(this),this._errorController=new Ke(this),this._errorController.addEventListener("slot-content-changed",e=>{this.toggleAttribute("has-error-message",e.detail.hasContent)}),this._labelController.addEventListener("slot-content-changed",e=>{let{hasContent:i,node:o}=e.detail;this.__labelChanged(i,o)}),this._helperController.addEventListener("slot-content-changed",e=>{let{hasContent:i,node:o}=e.detail;this.toggleAttribute("has-helper",i),this.__helperChanged(i,o)})}get _errorNode(){return this._errorController.node}get _helperNode(){return this._helperController.node}ready(){super.ready(),this.addController(this._fieldAriaController),this.addController(this._helperController),this.addController(this._errorController)}__helperChanged(e,i){e?this._fieldAriaController.setHelperId(i.id):this._fieldAriaController.setHelperId(null)}_accessibleNameChanged(e){this._fieldAriaController.setAriaLabel(e)}_accessibleNameRefChanged(e){this._fieldAriaController.setLabelId(e,!0)}__labelChanged(e,i){e?this._fieldAriaController.setLabelId(i.id):this._fieldAriaController.setLabelId(null)}_errorMessageChanged(e){this._errorController.setErrorMessage(e)}_helperTextChanged(e){this._helperController.setHelperText(e)}_ariaTargetChanged(e){e&&this._fieldAriaController.setTarget(e)}_requiredChanged(e){this._fieldAriaController.setRequired(e)}_invalidChanged(e){this._errorController.setInvalid(e),setTimeout(()=>{if(e){let i=this._errorNode;this._fieldAriaController.setErrorId(i?.id)}else this._fieldAriaController.setErrorId(null)})}};var ce=class extends A{constructor(t,e,i={}){let{uniqueIdPrefix:o}=i;super(t,"input","input",{initializer:(s,n)=>{n.value&&(s.value=n.value),n.type&&s.setAttribute("type",n.type),s.id=this.defaultId,typeof e=="function"&&e(s)},useUniqueId:!0,uniqueIdPrefix:o})}};var he=class{constructor(t,e){this.input=t,this.__preventDuplicateLabelClick=this.__preventDuplicateLabelClick.bind(this),e.addEventListener("slot-content-changed",i=>{this.__initLabel(i.detail.node)}),this.__initLabel(e.node)}__initLabel(t){t&&(t.addEventListener("click",this.__preventDuplicateLabelClick),this.input&&t.setAttribute("for",this.input.id))}__preventDuplicateLabelClick(){let t=e=>{e.stopImmediatePropagation(),this.input.removeEventListener("click",t)};this.input.addEventListener("click",t)}};var wr=r=>class extends je(de(Ue(ae(j(r))))){static get properties(){return{indeterminate:{type:Boolean,notify:!0,value:!1,reflectToAttribute:!0},name:{type:String,value:""},readonly:{type:Boolean,value:!1,reflectToAttribute:!0}}}static get observers(){return["__readonlyChanged(readonly, inputElement)"]}static get delegateProps(){return[...super.delegateProps,"indeterminate"]}static get delegateAttrs(){return[...super.delegateAttrs,"name","invalid","required"]}constructor(){super(),this._setType("checkbox"),this._boundOnInputClick=this._onInputClick.bind(this),this.value="on",this.tabindex=0}get slotStyles(){return[`
          ${this.localName} > input[slot='input'] {
            opacity: 0;
          }
        `]}ready(){super.ready(),this.addController(new ce(this,e=>{this._setInputElement(e),this._setFocusElement(e),this.stateTarget=e,this.ariaTarget=e})),this.addController(new he(this.inputElement,this._labelController)),this._createPropertyObserver("checked","_checkedChanged")}_shouldSetActive(e){let[i]=e.composedPath(),o=i===this.inputElement||i.part.contains("required-indicator")||this._labelNode.contains(i)&&!i.closest("a");return this.readonly||!o?!1:super._shouldSetActive(e)}_addInputListeners(e){super._addInputListeners(e),e.addEventListener("click",this._boundOnInputClick)}_removeInputListeners(e){super._removeInputListeners(e),e.removeEventListener("click",this._boundOnInputClick)}_onInputClick(e){this.readonly&&e.preventDefault()}__readonlyChanged(e,i){i&&(e?i.setAttribute("aria-readonly","true"):i.removeAttribute("aria-readonly"))}_toggleChecked(e){this.indeterminate&&(this.indeterminate=!1),super._toggleChecked(e)}checkValidity(){return!this.required||!!this.checked}_setFocused(e){super._setFocused(e),!e&&document.hasFocus()&&this._requestValidation()}_checkedChanged(e,i){(e||i)&&this._requestValidation()}_requiredChanged(e){super._requiredChanged(e),e===!1&&this._requestValidation()}_onRequiredIndicatorClick(){this._labelNode.click()}};var Gt=class extends wr(E(y(_(b(p))))){static get is(){return"vaadin-checkbox"}static get styles(){return fr}render(){return m`
      <div class="vaadin-checkbox-container">
        <div part="checkbox" aria-hidden="true"></div>
        <slot name="input"></slot>
        <div part="label">
          <slot name="label"></slot>
          <div part="required-indicator" @click="${this._onRequiredIndicatorClick}"></div>
        </div>
        <div part="helper-text">
          <slot name="helper"></slot>
        </div>
        <div part="error-message">
          <slot name="error-message"></slot>
        </div>
      </div>
      <slot name="tooltip"></slot>
    `}ready(){super.ready(),this._tooltipController=new I(this),this._tooltipController.setAriaTarget(this.inputElement),this.addController(this._tooltipController)}};v(Gt);var Xe=r=>r.test(navigator.userAgent),Xt=r=>r.test(navigator.platform),Ms=r=>r.test(navigator.vendor),Md=Xe(/Android/u),Td=Xe(/Chrome/u)&&Ms(/Google Inc/u),Id=Xe(/Firefox/u),Ts=Xt(/^iPad/u)||Xt(/^Mac/u)&&navigator.maxTouchPoints>1,Is=Xt(/^iPhone/u),Cr=Is||Ts,$d=Xe(/^((?!chrome|android).)*safari/iu),Od=(()=>{try{return document.createEvent("TouchEvent"),!0}catch{return!1}})();var Ye=class{saveFocus(t){this.focusNode=t||ye()}restoreFocus(t){let e=this.focusNode;if(!e)return;let i={preventScroll:t?t.preventScroll:!1,focusVisible:t?t.focusVisible:!1};ye()===document.body?setTimeout(()=>e.focus(i)):e.focus(i),this.focusNode=null}};var Yt=[];var Ze=class{constructor(t){this.host=t,this.__trapNode=null,this.__onKeyDown=this.__onKeyDown.bind(this)}get __focusableElements(){return pr(this.__trapNode)}get __focusedElementIndex(){let t=this.__focusableElements;return t.indexOf(t.filter(Re).pop())}hostConnected(){document.addEventListener("keydown",this.__onKeyDown)}hostDisconnected(){document.removeEventListener("keydown",this.__onKeyDown)}trapFocus(t){if(this.__trapNode=t,this.__focusableElements.length===0)throw this.__trapNode=null,new Error("The trap node should have at least one focusable descendant or be focusable itself.");Yt.push(this),this.__focusedElementIndex===-1&&this.__focusableElements[0].focus({focusVisible:V()})}releaseFocus(){this.__trapNode=null,Yt.pop()}__onKeyDown(t){if(this.__trapNode&&this===Array.from(Yt).pop()&&t.key==="Tab"){if(t.defaultPrevented)return;t.preventDefault();let e=t.shiftKey;this.__focusNextElement(e)}}__focusNextElement(t=!1){let e=this.__focusableElements,i=t?-1:1,o=this.__focusedElementIndex,s=(e.length+o+i)%e.length,n=e[s];n.focus({focusVisible:!0}),n.localName==="input"&&n.select()}};var Ar=r=>class extends r{static get properties(){return{focusTrap:{type:Boolean,value:!1},restoreFocusOnClose:{type:Boolean,value:!1},restoreFocusNode:{type:HTMLElement}}}constructor(){super(),this.__focusTrapController=new Ze(this),this.__focusRestorationController=new Ye}get _contentRoot(){return this}ready(){super.ready(),this.addController(this.__focusTrapController),this.addController(this.__focusRestorationController)}get _focusTrapRoot(){return this.$.overlay}_resetFocus(){if(this.focusTrap&&this.__focusTrapController.releaseFocus(),this.restoreFocusOnClose&&this._shouldRestoreFocus()){let e=V(),i=!e;this.__focusRestorationController.restoreFocus({preventScroll:i,focusVisible:e})}}_saveFocus(){this.restoreFocusOnClose&&this.__focusRestorationController.saveFocus(this.restoreFocusNode)}_trapFocus(){this.focusTrap&&!Q(this._focusTrapRoot)&&this.__focusTrapController.trapFocus(this._focusTrapRoot)}_shouldRestoreFocus(){let e=ye();return e===document.body||this._deepContains(e)}_deepContains(e){if(this._contentRoot.contains(e))return!0;let i=e,o=e.ownerDocument;for(;i&&i!==o&&i!==this._contentRoot;)i=i.parentNode||i.host;return i===this._contentRoot}};var Je=new Set,Qe=()=>[...Je].filter(r=>!r.hasAttribute("closing")),$s=r=>{let t=Qe(),e=t.indexOf(r);return e===-1?[]:t.slice(e+1)},Os=(r,t)=>r._deepContains(t),kr=(r,t=e=>!0)=>{let e=Qe().filter(t);return r===e.pop()},Er=r=>class extends r{get _last(){return kr(this)}get _isAttached(){return Je.has(this)}bringToFront(){if(kr(this))return;let e=$s(this),i=e.filter(o=>o._hasOverlayPositionMixin&&Os(this,o));i.length!==e.length&&[this,...i].forEach(o=>{o.matches(":popover-open")&&(o.hidePopover(),o.showPopover()),o._removeAttachedInstance(),o._appendAttachedInstance()})}_enterModalState(){document.body.style.pointerEvents!=="none"&&(this._previousDocumentPointerEvents=document.body.style.pointerEvents,document.body.style.pointerEvents="none"),Qe().forEach(e=>{e!==this&&e.toggleAttribute("suppressed",!0)})}_exitModalState(){this._previousDocumentPointerEvents!==void 0&&(document.body.style.pointerEvents=this._previousDocumentPointerEvents,delete this._previousDocumentPointerEvents);let e=Qe(),i;for(;(i=e.pop())&&!(i!==this&&(i.toggleAttribute("suppressed",!1),!i.modeless)););}_appendAttachedInstance(){Je.add(this)}_removeAttachedInstance(){this._isAttached&&Je.delete(this)}};function Sr(r,t){let e=null,i,o=document.documentElement;function s(){i&&clearTimeout(i),e?.disconnect(),e=null}function n(a=!1,l=1){s();let{left:d,top:f,width:h,height:w}=r.getBoundingClientRect();if(a||t(),!h||!w)return;let C=Math.floor(f),ee=Math.floor(o.clientWidth-(d+h)),oo=Math.floor(o.clientHeight-(f+w)),so=Math.floor(d),no={rootMargin:`${-C}px ${-ee}px ${-oo}px ${-so}px`,threshold:Math.max(0,Math.min(1,l))||1},fi=!0;function ao(lo){let rt=lo[0].intersectionRatio;if(rt!==l){if(!fi)return n();rt?n(!1,rt):i=setTimeout(()=>{n(!1,1e-7)},1e3)}fi=!1}e=new IntersectionObserver(ao,no),e.observe(r)}return n(!0),s}function k(r,t,e){let i=[r];r.owner&&i.push(r.owner),typeof e=="string"?i.forEach(o=>{o.setAttribute(t,e)}):e?i.forEach(o=>{o.setAttribute(t,"")}):i.forEach(o=>{o.removeAttribute(t)})}var et=r=>class extends Ar(Er(r)){static get properties(){return{opened:{type:Boolean,notify:!0,observer:"_openedChanged",reflectToAttribute:!0,sync:!0},owner:{type:Object,sync:!0},model:{type:Object,sync:!0},renderer:{type:Object,sync:!0},modeless:{type:Boolean,value:!1,reflectToAttribute:!0,observer:"_modelessChanged",sync:!0},hidden:{type:Boolean,reflectToAttribute:!0,observer:"_hiddenChanged",sync:!0},withBackdrop:{type:Boolean,value:!1,reflectToAttribute:!0,observer:"_withBackdropChanged",sync:!0}}}static get observers(){return["_rendererOrDataChanged(renderer, owner, model, opened)"]}get _rendererRoot(){return this}constructor(){super(),this._boundMouseDownListener=this._mouseDownListener.bind(this),this._boundMouseUpListener=this._mouseUpListener.bind(this),this._boundOutsideClickListener=this._outsideClickListener.bind(this),this._boundKeydownListener=this._keydownListener.bind(this),Cr&&(this._boundIosResizeListener=()=>this._detectIosNavbar())}firstUpdated(){super.firstUpdated(),this.popover="manual",this.addEventListener("click",()=>{}),this.$.backdrop&&this.$.backdrop.addEventListener("click",()=>{}),this.addEventListener("mouseup",()=>{document.activeElement===document.body&&this.$.overlay.getAttribute("tabindex")==="0"&&this.$.overlay.focus()}),this.addEventListener("animationcancel",()=>{this._flushAnimation("opening"),this._flushAnimation("closing")})}connectedCallback(){super.connectedCallback(),this._boundIosResizeListener&&(this._detectIosNavbar(),window.addEventListener("resize",this._boundIosResizeListener)),this.opened&&this._attachOverlay()}disconnectedCallback(){super.disconnectedCallback(),this.__scheduledOpen&&(cancelAnimationFrame(this.__scheduledOpen),this.__scheduledOpen=null),this._boundIosResizeListener&&window.removeEventListener("resize",this._boundIosResizeListener)}requestContentUpdate(){this.renderer&&this.renderer.call(this.owner,this._rendererRoot,this.owner,this.model)}close(e){let i=new CustomEvent("vaadin-overlay-close",{bubbles:!0,cancelable:!0,detail:{overlay:this,sourceEvent:e}});this.dispatchEvent(i),document.body.dispatchEvent(i),i.defaultPrevented||(this.opened=!1)}setBounds(e,i=!0){let o=this.$.overlay,s={...e};i&&o.style.position!=="absolute"&&(o.style.position="absolute"),Object.keys(s).forEach(n=>{s[n]!==null&&!isNaN(s[n])&&(s[n]=`${s[n]}px`)}),Object.assign(o.style,s)}_detectIosNavbar(){if(!this.opened)return;let e=window.innerHeight,o=window.innerWidth>e,s=document.documentElement.clientHeight;o&&s>e?this.style.setProperty("--vaadin-overlay-viewport-bottom",`${s-e}px`):this.style.setProperty("--vaadin-overlay-viewport-bottom","0px")}_shouldAddGlobalListeners(){return!this.modeless}_addGlobalListeners(){this.__hasGlobalListeners||(this.__hasGlobalListeners=!0,document.addEventListener("mousedown",this._boundMouseDownListener),document.addEventListener("mouseup",this._boundMouseUpListener),document.documentElement.addEventListener("click",this._boundOutsideClickListener,!0))}_removeGlobalListeners(){this.__hasGlobalListeners&&(this.__hasGlobalListeners=!1,document.removeEventListener("mousedown",this._boundMouseDownListener),document.removeEventListener("mouseup",this._boundMouseUpListener),document.documentElement.removeEventListener("click",this._boundOutsideClickListener,!0))}_rendererOrDataChanged(e,i,o,s){let n=this._oldOwner!==i||this._oldModel!==o;this._oldModel=o,this._oldOwner=i;let a=this._oldRenderer!==e,l=this._oldRenderer!==void 0;this._oldRenderer=e;let d=this._oldOpened!==s;this._oldOpened=s,a&&l&&(this._rendererRoot.innerHTML="",delete this._rendererRoot._$litPart$),s&&e&&(a||d||n)&&this.requestContentUpdate()}_modelessChanged(e){this.opened&&(this._shouldAddGlobalListeners()?this._addGlobalListeners():this._removeGlobalListeners()),e?this._exitModalState():this.opened&&this._enterModalState(),k(this,"modeless",e)}_withBackdropChanged(e){k(this,"with-backdrop",e)}_openedChanged(e,i){if(e){if(!this.isConnected){this.opened=!1;return}this._saveFocus(),this._animatedOpening(),this.__scheduledOpen=requestAnimationFrame(()=>{setTimeout(()=>{this._trapFocus();let o=new CustomEvent("vaadin-overlay-open",{detail:{overlay:this},bubbles:!0});this.dispatchEvent(o),document.body.dispatchEvent(o)})}),document.addEventListener("keydown",this._boundKeydownListener),this._shouldAddGlobalListeners()&&this._addGlobalListeners()}else i&&(this.__scheduledOpen&&(cancelAnimationFrame(this.__scheduledOpen),this.__scheduledOpen=null),this._resetFocus(),this._animatedClosing(),document.removeEventListener("keydown",this._boundKeydownListener),this._shouldAddGlobalListeners()&&this._removeGlobalListeners())}_hiddenChanged(e){e&&this.hasAttribute("closing")&&this._flushAnimation("closing")}_shouldAnimate(){let e=getComputedStyle(this),i=e.getPropertyValue("animation-name");return!(e.getPropertyValue("display")==="none")&&i&&i!=="none"}_enqueueAnimation(e,i){let o=`__${e}Handler`,s=n=>{n&&n.target!==this||(i(),this.removeEventListener("animationend",s),delete this[o])};this[o]=s,this.addEventListener("animationend",s)}_flushAnimation(e){let i=`__${e}Handler`;typeof this[i]=="function"&&this[i]()}_animatedOpening(){this._isAttached&&this.hasAttribute("closing")&&this._flushAnimation("closing"),this._attachOverlay(),this._appendAttachedInstance(),this.bringToFront(),this.modeless||this._enterModalState(),k(this,"opening",!0),this._shouldAnimate()?this._enqueueAnimation("opening",()=>{this._finishOpening()}):this._finishOpening()}_attachOverlay(){this.matches(":popover-open")||this.showPopover()}_finishOpening(){k(this,"opening",!1)}_finishClosing(){this._detachOverlay(),this._removeAttachedInstance(),this.toggleAttribute("suppressed",!1),k(this,"closing",!1),this.dispatchEvent(new CustomEvent("vaadin-overlay-closed"))}_animatedClosing(){this.hasAttribute("opening")&&this._flushAnimation("opening"),this._isAttached&&(this._exitModalState(),k(this,"closing",!0),this.dispatchEvent(new CustomEvent("vaadin-overlay-closing")),this._shouldAnimate()?this._enqueueAnimation("closing",()=>{this._finishClosing()}):this._finishClosing())}_detachOverlay(){this.hidePopover()}_mouseDownListener(e){this._mouseDownInside=e.composedPath().indexOf(this.$.overlay)>=0}_mouseUpListener(e){this._mouseUpInside=e.composedPath().indexOf(this.$.overlay)>=0}_shouldCloseOnOutsideClick(e){return this._last}_outsideClickListener(e){if(e.composedPath().includes(this.$.overlay)||this._mouseDownInside||this._mouseUpInside){this._mouseDownInside=!1,this._mouseUpInside=!1;return}if(!this._shouldCloseOnOutsideClick(e))return;let i=new CustomEvent("vaadin-overlay-outside-click",{cancelable:!0,detail:{sourceEvent:e}});this.dispatchEvent(i),this.opened&&!i.defaultPrevented&&(this.close(e),!this.opened&&!this.modeless&&e.preventDefault())}_keydownListener(e){if(!(!this._last||e.defaultPrevented)&&!(!this._shouldAddGlobalListeners()&&!e.composedPath().includes(this._focusTrapRoot))&&e.key==="Escape"){let i=new CustomEvent("vaadin-overlay-escape-press",{cancelable:!0,detail:{sourceEvent:e}});this.dispatchEvent(i),this.opened&&!i.defaultPrevented&&this.close(e)}}};var xe=c`
  :host {
    z-index: 200;
    position: fixed;

    /* Despite of what the names say, <vaadin-overlay> is just a container
          for position/sizing/alignment. The actual overlay is the overlay part. */

    /* Default position constraints. Themes can
          override this to adjust the gap between the overlay and the viewport. */
    inset: max(env(safe-area-inset-top, 0px), var(--vaadin-overlay-viewport-inset, 8px))
      max(env(safe-area-inset-right, 0px), var(--vaadin-overlay-viewport-inset, 8px))
      max(env(safe-area-inset-bottom, 0px), var(--vaadin-overlay-viewport-bottom))
      max(env(safe-area-inset-left, 0px), var(--vaadin-overlay-viewport-inset, 8px));

    /* Override native [popover] user agent styles */
    width: auto;
    height: auto;
    border: none;
    padding: 0;
    background-color: transparent;
    overflow: visible;

    /* Use flexbox alignment for the overlay part. */
    display: flex;
    flex-direction: column; /* makes dropdowns sizing easier */
    /* Align to center by default. */
    align-items: center;
    justify-content: center;

    /* Allow centering when max-width/max-height applies. */
    margin: auto;

    /* The host is not clickable, only the overlay part is. */
    pointer-events: none;

    /* Remove tap highlight on touch devices. */
    -webkit-tap-highlight-color: transparent;

    /* CSS API for host */
    --vaadin-overlay-viewport-bottom: 8px;
  }

  :host([hidden]),
  :host(:not([opened]):not([closing])),
  :host(:not([opened]):not([closing])) [part='overlay'] {
    display: none !important;
  }

  :host([suppressed]) [part='overlay'],
  :host([suppressed]) ::slotted(*) {
    pointer-events: none !important;
  }

  [part='overlay'] {
    color: var(--vaadin-overlay-text-color, var(--vaadin-text-color));
    background: var(--vaadin-overlay-background, var(--vaadin-background-color));
    border: var(--vaadin-overlay-border-width, 1px) solid
      var(--vaadin-overlay-border-color, var(--vaadin-border-color-secondary));
    border-radius: var(--vaadin-overlay-border-radius, var(--vaadin-radius-m));
    box-shadow: var(--vaadin-overlay-shadow, 0 8px 24px -4px rgba(0, 0, 0, 0.3));
    box-sizing: border-box;
    max-width: 100%;
    overflow: auto;
    overscroll-behavior: contain;
    pointer-events: auto;
    -webkit-tap-highlight-color: initial;

    /* CSS reset for font styles */
    font: initial;
    letter-spacing: initial;
    text-align: initial;
    text-decoration: initial;
    text-indent: initial;
    text-transform: initial;
    user-select: text;
    white-space: initial;
    word-spacing: initial;

    /* Inherit font-family */
    font-family: inherit;
  }

  [part='backdrop'] {
    background: var(--vaadin-overlay-backdrop-background, rgba(0, 0, 0, 0.2));
    content: '';
    inset: 0;
    pointer-events: auto;
    position: fixed;
    z-index: -1;
  }

  [part='overlay']:focus-visible {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
  }

  @media (forced-colors: active) {
    [part='overlay'] {
      border: 3px solid !important;
    }
  }
`;var Mr=c`
  /* Optical centering */
  :host::before,
  :host::after {
    content: '';
    flex-basis: 0;
    flex-grow: 1;
  }

  :host::after {
    flex-grow: 1.1;
  }

  :host {
    cursor: default;
    --_overflow-indicator-height: var(--vaadin-dialog-overflow-indicator-height, 1px);
    --_overflow-indicator-color: var(--vaadin-dialog-overflow-indicator-color, var(--vaadin-border-color-secondary));
  }

  [part='overlay']:focus-visible {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
  }

  [part='overlay'] {
    color: var(--vaadin-dialog-text-color, var(--vaadin-overlay-text-color, var(--vaadin-text-color)));
    background: var(--vaadin-dialog-background, var(--vaadin-overlay-background, var(--vaadin-background-color)));
    background-origin: border-box;
    border: var(--vaadin-dialog-border-width, var(--vaadin-overlay-border-width, 1px)) solid
      var(--vaadin-dialog-border-color, var(--vaadin-overlay-border-color, var(--vaadin-border-color-secondary)));
    box-shadow: var(--vaadin-dialog-shadow, var(--vaadin-overlay-shadow, 0 8px 24px -4px rgba(0, 0, 0, 0.3)));
    border-radius: var(--vaadin-dialog-border-radius, var(--vaadin-radius-l));
    width: max-content;
    min-width: min(var(--vaadin-dialog-min-width, 4em), 100%);
    max-width: min(var(--vaadin-dialog-max-width, 100%), 100%);
    max-height: 100%;
  }

  [part='header'],
  [part='header-content'],
  [part='footer'] {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    flex: none;
    pointer-events: none;
    z-index: 1;
    gap: var(--vaadin-dialog-toolbar-gap, var(--vaadin-gap-s));
  }

  ::slotted(*) {
    pointer-events: auto;
  }

  [part='header'],
  [part='content'],
  [part='footer'] {
    padding: var(--vaadin-dialog-padding, var(--vaadin-padding-l));
  }

  :host([theme~='no-padding']) [part='content'] {
    padding: 0 !important;
  }

  :host(:is([has-header], [has-title])) [part='content'] {
    padding-top: 0;
  }

  :host([has-footer]) [part='content'] {
    padding-bottom: 0;
  }

  [part='header'] {
    flex-wrap: nowrap;
  }

  ::slotted([slot='header-content']),
  ::slotted([slot='title']),
  ::slotted([slot='footer']) {
    display: contents;
  }

  ::slotted([slot='title']) {
    font: inherit !important;
    color: inherit !important;
    overflow-wrap: anywhere;
  }

  [part='title'] {
    color: var(--vaadin-dialog-title-color, var(--vaadin-text-color));
    font-weight: var(--vaadin-dialog-title-font-weight, 600);
    font-size: var(--vaadin-dialog-title-font-size, 1em);
    line-height: var(--vaadin-dialog-title-line-height, inherit);
  }

  [part='header-content'] {
    flex: 1;
  }

  :host([has-title]) [part='header-content'],
  [part='footer'] {
    justify-content: flex-end;
  }

  :host(:not([has-title]):not([has-header])) [part='header'],
  :host(:not([has-header])) [part='header-content'],
  :host(:not([has-title])) [part='title'],
  :host(:not([has-footer])) [part='footer'] {
    display: none !important;
  }

  [part='header'],
  [part='footer'] {
    position: relative;

    &::after {
      content: '';
      opacity: 0;
      position: absolute;
      pointer-events: none;
      height: var(--_overflow-indicator-height);
      top: 100%;
      inset-inline: 0;
      background: linear-gradient(
        var(--_overflow-indicator-dir, to bottom),
        var(--_overflow-indicator-color),
        var(--_overflow-indicator-color) 1px,
        transparent
      );
    }
  }

  [part='footer']::after {
    top: auto;
    bottom: 100%;
    --_overflow-indicator-dir: to top;
  }

  :host([overflow~='top']) [part='header']::after,
  :host([overflow~='bottom']) [part='footer']::after {
    opacity: 1;
  }
`,oc=c`
  [part='overlay'] {
    position: relative;
    overflow: visible;
    display: flex;
  }

  :host([has-bounds-set]) [part='overlay'] {
    min-width: 0;
  }

  :host([has-bounds-set]:not([keep-in-viewport])) [part='overlay'] {
    max-width: none;
    max-height: none;
  }

  /* Content part scrolls by default */
  [part='content'] {
    flex: 1;
    min-height: 0;
    overflow: auto;
    overscroll-behavior: contain;
    clip-path: border-box;
  }

  [part='header'],
  :host(:not([has-title], [has-header])) [part='content'] {
    border-top-left-radius: inherit;
    border-top-right-radius: inherit;
  }

  [part='footer'],
  :host(:not([has-footer])) [part='content'] {
    border-bottom-left-radius: inherit;
    border-bottom-right-radius: inherit;
  }

  .resizer-container {
    display: flex;
    flex-direction: column;
    flex-grow: 1;
    max-width: 100%;
    border-radius: calc(
      var(--vaadin-dialog-border-radius, var(--vaadin-radius-l)) - var(
          --vaadin-dialog-border-width,
          var(--vaadin-overlay-border-width, 1px)
        )
    );
  }

  :host(:not([resizable])) .resizer {
    display: none;
  }

  .resizer {
    position: absolute;
    height: 16px;
    width: 16px;
  }

  .resizer.edge {
    height: 8px;
    width: 8px;
    inset: -4px;
  }

  .resizer.edge.n {
    width: auto;
    bottom: auto;
    cursor: ns-resize;
  }

  .resizer.ne {
    top: -4px;
    right: -4px;
    cursor: nesw-resize;
  }

  .resizer.edge.e {
    height: auto;
    left: auto;
    cursor: ew-resize;
  }

  .resizer.se {
    bottom: -4px;
    right: -4px;
    cursor: nwse-resize;
  }

  .resizer.edge.s {
    width: auto;
    top: auto;
    cursor: ns-resize;
  }

  .resizer.sw {
    bottom: -4px;
    left: -4px;
    cursor: nesw-resize;
  }

  .resizer.edge.w {
    height: auto;
    right: auto;
    cursor: ew-resize;
  }

  .resizer.nw {
    top: -4px;
    left: -4px;
    cursor: nwse-resize;
  }
`;var Ls=c`
  :host {
    --vaadin-dialog-min-width: var(--vaadin-confirm-dialog-min-width, 15em);
    --vaadin-dialog-max-width: var(--vaadin-confirm-dialog-max-width, 25em);
  }

  ::slotted([slot='header']) {
    display: contents;
    font: inherit !important;
    color: inherit !important;
  }

  [part='header'] {
    color: var(--vaadin-dialog-title-color, var(--vaadin-text-color));
    font-weight: var(--vaadin-dialog-title-font-weight, 600);
    font-size: var(--vaadin-dialog-title-font-size, 1em);
    line-height: var(--vaadin-dialog-title-line-height, inherit);
  }

  [part='overlay'] {
    display: flex;
    flex-direction: column;
  }

  [part='content'] {
    flex: 1;
  }
`,Tr=[xe,Mr,Ls];var Zt=class extends et(S(y(_(b(p))))){static get is(){return"vaadin-confirm-dialog-overlay"}static get styles(){return Tr}static get properties(){return{cancelButtonVisible:{type:Boolean,value:!1},rejectButtonVisible:{type:Boolean,value:!1}}}render(){return m`
      <div part="backdrop" id="backdrop" ?hidden="${!this.withBackdrop}"></div>
      <div part="overlay" id="overlay">
        <header part="header"><slot name="header"></slot></header>
        <div part="content" id="content">
          <div part="message"><slot></slot></div>
        </div>
        <footer part="footer" role="toolbar">
          <div part="cancel-button" ?hidden="${!this.cancelButtonVisible}">
            <slot name="cancel-button"></slot>
          </div>
          <div part="reject-button" ?hidden="${!this.rejectButtonVisible}">
            <slot name="reject-button"></slot>
          </div>
          <div part="confirm-button">
            <slot name="confirm-button"></slot>
          </div>
        </footer>
      </div>
    `}ready(){super.ready(),this.setAttribute("has-header",""),this.setAttribute("has-footer","")}get _contentRoot(){return this.owner}get _focusTrapRoot(){return this.owner}};v(Zt);var we=r=>r??g;var Ir=r=>class extends r{static get properties(){return{width:{type:String},height:{type:String}}}static get observers(){return["__sizeChanged(width, height)"]}__sizeChanged(e,i){requestAnimationFrame(()=>this.$.overlay.setBounds({width:e,height:i},!1))}};var $r=r=>class extends Ir(r){static get properties(){return{accessibleDescriptionRef:{type:String},opened:{type:Boolean,reflectToAttribute:!0,value:!1,notify:!0,sync:!0},header:{type:String,value:""},message:{type:String,value:""},confirmText:{type:String,value:"Confirm"},confirmTheme:{type:String,value:"primary"},noCloseOnEsc:{type:Boolean,value:!1},rejectButtonVisible:{type:Boolean,reflectToAttribute:!0,value:!1},rejectText:{type:String,value:"Reject"},rejectTheme:{type:String,value:"error tertiary"},cancelButtonVisible:{type:Boolean,reflectToAttribute:!0,value:!1},cancelText:{type:String,value:"Cancel"},cancelTheme:{type:String,value:"tertiary"},_cancelButton:{type:Object},_confirmButton:{type:Object},_headerNode:{type:Object},_messageNodes:{type:Array,value:()=>[]},_rejectButton:{type:Object}}}static get observers(){return["__updateConfirmButton(_confirmButton, confirmText, confirmTheme)","__updateCancelButton(_cancelButton, cancelText, cancelTheme, cancelButtonVisible)","__updateHeaderNode(_headerNode, header)","__updateMessageNodes(_messageNodes, message)","__updateRejectButton(_rejectButton, rejectText, rejectTheme, rejectButtonVisible)","__accessibleDescriptionRefChanged(_messageNodes, accessibleDescriptionRef)"]}constructor(){super(),this.__cancel=this.__cancel.bind(this),this.__confirm=this.__confirm.bind(this),this.__reject=this.__reject.bind(this)}connectedCallback(){super.connectedCallback(),this.__restoreOpened&&(this.opened=!0)}disconnectedCallback(){super.disconnectedCallback(),setTimeout(()=>{this.isConnected||(this.__restoreOpened=this.opened,this.opened=!1)})}ready(){super.ready(),this.role="alertdialog",this.setAttribute("aria-modal","true"),this.setAttribute("tabindex","0"),this._headerController=new A(this,"header","h3",{initializer:e=>{this._headerNode=e}}),this.addController(this._headerController),this._messageController=new A(this,"","div",{multiple:!0,observe:!1,initializer:e=>{this._messageNodes=[...this._messageNodes,e]}}),this.addController(this._messageController),this._cancelController=new A(this,"cancel-button","vaadin-button",{initializer:e=>{this.__setupSlottedButton("cancel",e)}}),this.addController(this._cancelController),this._rejectController=new A(this,"reject-button","vaadin-button",{initializer:e=>{this.__setupSlottedButton("reject",e)}}),this.addController(this._rejectController),this._confirmController=new A(this,"confirm-button","vaadin-button",{initializer:e=>{this.__setupSlottedButton("confirm",e)}}),this.addController(this._confirmController),this._overlayElement=this.$.overlay}updated(e){super.updated(e),e.has("header")&&(this.ariaLabel=this.header||"confirmation")}__onDialogOpened(){this._confirmButton&&this._confirmButton.focus()}__onDialogClosed(){this.dispatchEvent(new CustomEvent("closed"))}__accessibleDescriptionRefChanged(e,i){if(e){if(i)this.removeAttribute("aria-description"),q(this,"aria-describedby",{newId:i,oldId:this.__oldAccessibleDescriptionRef,fromUser:!0});else{this.removeAttribute("aria-describedby");let o=e.map(s=>s.textContent.trim()).join(" ");this.setAttribute("aria-description",o)}this.__oldAccessibleDescriptionRef=i}}__setupSlottedButton(e,i){let o=`_${e}Button`,s=`__${e}`;this[o]&&this[o]!==i&&this[o].remove(),i.addEventListener("click",this[s]),this[o]=i}__updateCancelButton(e,i,o,s){e&&(e===this._cancelController.defaultNode&&(e.textContent=i,e.setAttribute("theme",o)),e.toggleAttribute("hidden",!s))}__updateConfirmButton(e,i,o){e&&e===this._confirmController.defaultNode&&(e.textContent=i,e.setAttribute("theme",o))}__updateHeaderNode(e,i){e&&e===this._headerController.defaultNode&&(e.textContent=i)}__updateMessageNodes(e,i){if(e?.length>0){let o=e.find(s=>s===this._messageController.defaultNode);o&&(o.textContent=i)}}__updateRejectButton(e,i,o,s){e&&(e===this._rejectController.defaultNode&&(e.textContent=i,e.setAttribute("theme",o)),e.toggleAttribute("hidden",!s))}_onOverlayEscapePress(e){this.noCloseOnEsc?e.preventDefault():this.__cancel()}_onOverlayOutsideClick(e){e.preventDefault()}__confirm(){this.dispatchEvent(new CustomEvent("confirm")),this.opened=!1}__cancel(){this.dispatchEvent(new CustomEvent("cancel")),this.opened=!1}__reject(){this.dispatchEvent(new CustomEvent("reject")),this.opened=!1}};var Jt=class extends $r(E(Pe(_(p)))){static get is(){return"vaadin-confirm-dialog"}static get styles(){return c`
      :host([opened]),
      :host([opening]),
      :host([closing]) {
        display: block !important;
        position: fixed;
        outline: none;
      }

      :host,
      :host([hidden]) {
        display: none !important;
      }

      :host(:focus-visible) ::part(overlay) {
        outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
      }
    `}render(){return m`
      <vaadin-confirm-dialog-overlay
        id="overlay"
        .owner="${this}"
        .opened="${this.opened}"
        theme="${we(this._theme)}"
        .cancelButtonVisible="${this.cancelButtonVisible}"
        .rejectButtonVisible="${this.rejectButtonVisible}"
        with-backdrop
        restore-focus-on-close
        focus-trap
        exportparts="backdrop, overlay, header, content, message, footer, cancel-button, confirm-button, reject-button"
        @opened-changed="${this._onOpenedChanged}"
        @vaadin-overlay-open="${this.__onDialogOpened}"
        @vaadin-overlay-closed="${this.__onDialogClosed}"
        @vaadin-overlay-outside-click="${this._onOverlayOutsideClick}"
        @vaadin-overlay-escape-press="${this._onOverlayEscapePress}"
      >
        <slot name="header" slot="header"></slot>
        <slot></slot>
        <slot name="cancel-button" slot="cancel-button"></slot>
        <slot name="reject-button" slot="reject-button"></slot>
        <slot name="confirm-button" slot="confirm-button"></slot>
      </vaadin-confirm-dialog-overlay>
    `}_onOpenedChanged(t){this.opened=t.detail.value}};v(Jt);var Or=c`
  :host {
    display: block;
    width: 100%; /* prevent collapsing inside non-stretching column flex */
    height: var(--vaadin-progress-bar-height, 0.5lh);
    contain: layout size;
  }

  :host([hidden]) {
    display: none !important;
  }

  [part='bar'] {
    box-sizing: border-box;
    height: 100%;
    --_padding: var(--vaadin-progress-bar-padding, 0px);
    padding: var(--_padding);
    background: var(--vaadin-progress-bar-background, var(--vaadin-background-container));
    border-radius: var(--vaadin-progress-bar-border-radius, var(--vaadin-radius-m));
    border: var(--vaadin-progress-bar-border-width, 1px) solid
      var(--vaadin-progress-bar-border-color, var(--vaadin-border-color-secondary));
  }

  [part='value'] {
    box-sizing: border-box;
    height: 100%;
    width: calc(var(--vaadin-progress-value) * 100%);
    background: var(--vaadin-progress-bar-value-background, var(--vaadin-border-color));
    border-radius: calc(
      var(--vaadin-progress-bar-border-radius, var(--vaadin-radius-m)) - var(
          --vaadin-progress-bar-border-width,
          1px
        ) - var(--_padding)
    );
    transition: width 150ms;
  }

  /* Indeterminate progress */
  :host([indeterminate]) [part='value'] {
    --_w-min: clamp(8px, 5%, 16px);
    --_w-max: clamp(16px, 20%, 128px);
    animation: indeterminate var(--vaadin-progress-bar-animation-duration, 1s) linear infinite alternate;
    width: var(--_w-min);
  }

  :host([indeterminate][aria-valuenow]) [part='value'] {
    animation-delay: 150ms;
  }

  @keyframes indeterminate {
    0% {
      animation-timing-function: ease-in;
    }

    20% {
      margin-inline-start: 0%;
      width: var(--_w-max);
    }

    50% {
      margin-inline-start: calc(50% - var(--_w-max) / 2);
    }

    80% {
      width: var(--_w-max);
      margin-inline-start: calc(100% - var(--_w-max));
      animation-timing-function: ease-out;
    }

    100% {
      width: var(--_w-min);
      margin-inline-start: calc(100% - var(--_w-min));
    }
  }

  @keyframes indeterminate-reduced {
    100% {
      opacity: 0.2;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    [part='value'] {
      transition: none;
    }

    :host([indeterminate]) [part='value'] {
      width: 25%;
      animation: indeterminate-reduced 2s linear infinite alternate;
    }
  }

  @media (forced-colors: active) {
    [part='bar'] {
      border-width: max(1px, var(--vaadin-progress-bar-border-width));
    }

    [part='value'] {
      background: CanvasText !important;
    }
  }
`;var Lr=r=>class extends r{static get properties(){return{value:{type:Number,observer:"_valueChanged"},min:{type:Number,value:0,observer:"_minChanged"},max:{type:Number,value:1,observer:"_maxChanged"},indeterminate:{type:Boolean,value:!1,reflectToAttribute:!0}}}static get observers(){return["_normalizedValueChanged(value, min, max)"]}ready(){super.ready(),this.setAttribute("role","progressbar")}_normalizedValueChanged(e,i,o){let s=this._normalizeValue(e,i,o);this.style.setProperty("--vaadin-progress-value",s)}_valueChanged(e){this.setAttribute("aria-valuenow",e)}_minChanged(e){this.setAttribute("aria-valuemin",e)}_maxChanged(e){this.setAttribute("aria-valuemax",e)}_normalizeValue(e,i,o){let s;return!e&&e!==0?s=0:i>=o?s=1:(s=(e-i)/(o-i),s=Math.min(Math.max(s,0),1)),s}};var Qt=class extends Lr(E(y(_(b(p))))){static get is(){return"vaadin-progress-bar"}static get styles(){return Or}render(){return m`
      <div part="bar">
        <div part="value"></div>
      </div>
    `}};v(Qt);var Ns=c`
  [part='radio'] {
    border-radius: 50%;
    color: var(--vaadin-radio-button-dot-color, var(--_color));
  }

  [part='radio']::after {
    width: var(--vaadin-radio-button-dot-size, var(--vaadin-radio-button-marker-size, 50%));
    height: var(--vaadin-radio-button-dot-size, var(--vaadin-radio-button-marker-size, 50%));
    border-radius: 50%;
    filter: var(--vaadin-radio-button-dot-color, var(--_filter));
  }
`,Nr=[H,ze("radio","radio-button"),Ns];var Pr=r=>class extends je(Ge(Ue(ae(j(r))))){static get properties(){return{name:{type:String,value:""}}}static get delegateAttrs(){return[...super.delegateAttrs,"name"]}constructor(){super(),this._setType("radio"),this.value="on",this.tabindex=0}get slotStyles(){return[`
          ${this.localName} > input[slot='input'] {
            opacity: 0;
          }
        `]}ready(){super.ready(),this.addController(new ce(this,e=>{this._setInputElement(e),this._setFocusElement(e),this.stateTarget=e,this.ariaTarget=e})),this.addController(new he(this.inputElement,this._labelController))}};var ei=class extends Pr(E(y(_(b(p))))){static get is(){return"vaadin-radio-button"}static get styles(){return Nr}render(){return m`
      <div class="vaadin-radio-button-container">
        <div part="radio" aria-hidden="true"></div>
        <slot name="input"></slot>
        <slot name="label"></slot>
      </div>
    `}};v(ei);var Dr=c`
  [part='label'],
  [part='helper-text'],
  [part='error-message'] {
    width: auto;
    min-width: auto;
  }

  [part='group-field'] {
    display: flex;
    flex-direction: column;
    gap: var(--vaadin-gap-xs) var(--vaadin-gap-xl);
  }

  :host([theme~='horizontal']) [part='group-field'] {
    flex-flow: row wrap;
    align-items: center;
  }

  :host([has-label][theme~='horizontal']) [part='group-field'] {
    padding: var(--vaadin-padding-block-container) var(--vaadin-padding-inline-container);
    padding-inline: 0;
    border-block: var(--vaadin-input-field-border-width, 1px) solid transparent;
  }
`;var Rr=[H,Dr,c`
    :host([readonly]) ::slotted(vaadin-radio-button) {
      --vaadin-radio-button-background: transparent;
      --vaadin-radio-button-border-color: var(--vaadin-border-color);
      --vaadin-radio-button-marker-color: var(--vaadin-text-color);
      --_border-style: dashed;
    }
  `];var Br=r=>class extends de(U(F(z(r)))){static get properties(){return{name:{type:String,observer:"__nameChanged",sync:!0},value:{type:String,notify:!0,value:"",sync:!0,observer:"__valueChanged"},readonly:{type:Boolean,value:!1,reflectToAttribute:!0,sync:!0,observer:"__readonlyChanged"},_fieldName:{type:String}}}constructor(){super(),this.__registerRadioButton=this.__registerRadioButton.bind(this),this.__unregisterRadioButton=this.__unregisterRadioButton.bind(this),this.__onRadioButtonCheckedChange=this.__onRadioButtonCheckedChange.bind(this),this._tooltipController=new I(this),this._tooltipController.addEventListener("tooltip-changed",e=>{if(e.detail.node?.isConnected){let o=this.__radioButtons.map(s=>s.inputElement);this._tooltipController.setAriaTarget(o)}else this._tooltipController.setAriaTarget([])})}get __radioButtons(){return this.__filterRadioButtons([...this.children])}get __selectedRadioButton(){return this.__radioButtons.find(e=>e.checked)}get isHorizontalRTL(){return this.__isRTL&&this._theme!=="vertical"}ready(){super.ready(),this.ariaTarget=this,this.setAttribute("role","radiogroup"),this._fieldName=`${this.localName}-${se()}`;let e=this.shadowRoot.querySelector("slot:not([name])");this._observer=new R(e,({addedNodes:i,removedNodes:o})=>{this.__filterRadioButtons(i).reverse().forEach(this.__registerRadioButton),this.__filterRadioButtons(o).forEach(this.__unregisterRadioButton);let s=this.__radioButtons.map(n=>n.inputElement);this._tooltipController.setAriaTarget(s)}),this.addController(this._tooltipController)}__filterRadioButtons(e){return e.filter(i=>i.nodeType===Node.ELEMENT_NODE&&i.localName==="vaadin-radio-button")}_onKeyDown(e){super._onKeyDown(e);let i=e.composedPath().find(o=>o.nodeType===Node.ELEMENT_NODE&&o.localName==="vaadin-radio-button");["ArrowLeft","ArrowUp"].includes(e.key)&&(e.preventDefault(),this.__selectNextRadioButton(i)),["ArrowRight","ArrowDown"].includes(e.key)&&(e.preventDefault(),this.__selectPrevRadioButton(i))}_invalidChanged(e){super._invalidChanged(e),e?this.setAttribute("aria-invalid","true"):this.removeAttribute("aria-invalid")}__nameChanged(e){this.__radioButtons.forEach(i=>{i.name=e||this._fieldName})}__selectNextRadioButton(e){let i=this.__radioButtons.indexOf(e);this.__selectIncRadioButton(i,this.isHorizontalRTL?1:-1)}__selectPrevRadioButton(e){let i=this.__radioButtons.indexOf(e);this.__selectIncRadioButton(i,this.isHorizontalRTL?-1:1)}__selectIncRadioButton(e,i){let o=(this.__radioButtons.length+e+i)%this.__radioButtons.length,s=this.__radioButtons[o];s.disabled?this.__selectIncRadioButton(o,i):(s.focusElement.focus(),s.focusElement.click())}__registerRadioButton(e){e.name=this.name||this._fieldName,e.addEventListener("checked-changed",this.__onRadioButtonCheckedChange),(this.disabled||this.readonly)&&(e.disabled=!0),e.checked&&this.__selectRadioButton(e)}__unregisterRadioButton(e){e.removeEventListener("checked-changed",this.__onRadioButtonCheckedChange),e.value===this.value&&this.__selectRadioButton(null)}__onRadioButtonCheckedChange(e){e.target.checked&&this.__selectRadioButton(e.target)}__valueChanged(e,i){if(!(i===void 0&&e==="")){if(e){let o=this.__radioButtons.find(s=>s.value===e);o?(this.__selectRadioButton(o),this.toggleAttribute("has-value",!0)):console.warn(`The radio button with the value "${e}" was not found.`)}else this.__selectRadioButton(null),this.removeAttribute("has-value");i!==void 0&&this._requestValidation()}}__readonlyChanged(e,i){!e&&i===void 0||i!==e&&this.__updateRadioButtonsDisabledProperty()}_disabledChanged(e,i){super._disabledChanged(e,i),!(!e&&i===void 0)&&i!==e&&this.__updateRadioButtonsDisabledProperty()}_shouldRemoveFocus(e){return!this.contains(e.relatedTarget)}_setFocused(e){super._setFocused(e),!e&&document.hasFocus()&&this._requestValidation()}__selectRadioButton(e){e?this.value=e.value:this.value="",this.__radioButtons.forEach(i=>{i.checked=i===e}),this.readonly&&this.__updateRadioButtonsDisabledProperty()}__updateRadioButtonsDisabledProperty(){this.__radioButtons.forEach(e=>{this.readonly?e.disabled=e!==this.__selectedRadioButton:e.disabled=this.disabled})}};var ti=class extends Br(E(y(_(b(p))))){static get is(){return"vaadin-radio-group"}static get styles(){return Rr}render(){return m`
      <div class="vaadin-group-field-container">
        <div part="label">
          <slot name="label"></slot>
          <span part="required-indicator" aria-hidden="true"></span>
        </div>

        <div part="group-field">
          <slot></slot>
        </div>

        <div part="helper-text">
          <slot name="helper"></slot>
        </div>

        <div part="error-message">
          <slot name="error-message"></slot>
        </div>
      </div>

      <slot name="tooltip"></slot>
    `}};v(ti);var Fr=c`
  :host {
    display: flex;
    align-items: center;
    --_radius: var(--vaadin-input-field-border-radius, var(--vaadin-radius-m));
    border-radius:
      /* See https://developer.mozilla.org/en-US/docs/Web/CSS/border-radius */
      var(--vaadin-input-field-top-start-radius, var(--_radius))
      var(--vaadin-input-field-top-end-radius, var(--_radius))
      var(--vaadin-input-field-bottom-end-radius, var(--_radius))
      var(--vaadin-input-field-bottom-start-radius, var(--_radius));
    border: var(--vaadin-input-field-border-width, 1px) solid
      var(--vaadin-input-field-border-color, var(--vaadin-border-color));
    box-sizing: border-box;
    cursor: text;
    padding: var(
      --vaadin-input-field-padding,
      var(--vaadin-padding-block-container) var(--vaadin-padding-inline-container)
    );
    gap: var(--vaadin-input-field-gap, var(--vaadin-gap-s));
    background: var(--vaadin-input-field-background, var(--vaadin-background-color));
    color: var(--vaadin-input-field-value-color, var(--vaadin-text-color));
    font-size: var(--vaadin-input-field-value-font-size, inherit);
    line-height: var(--vaadin-input-field-value-line-height, inherit);
    font-weight: var(--vaadin-input-field-value-font-weight, 400);
  }

  :host([dir='rtl']) {
    --_radius: var(--vaadin-input-field-border-radius, var(--vaadin-radius-m));
    border-radius:
      /* Don't use logical props, see https://github.com/vaadin/vaadin-time-picker/issues/145 */
      var(--vaadin-input-field-top-end-radius, var(--_radius))
      var(--vaadin-input-field-top-start-radius, var(--_radius))
      var(--vaadin-input-field-bottom-start-radius, var(--_radius))
      var(--vaadin-input-field-bottom-end-radius, var(--_radius));
  }

  :host([hidden]) {
    display: none !important;
  }

  /* Reset the native input styles */
  ::slotted(:is(input, textarea)) {
    appearance: none;
    align-self: stretch;
    box-sizing: border-box;
    flex: auto;
    white-space: nowrap;
    overflow: hidden;
    width: 100%;
    height: auto;
    outline: none;
    margin: 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    min-width: 0;
    font: inherit;
    font-size: 1em;
    color: inherit;
    background: transparent;
    cursor: inherit;
    text-align: inherit;
    caret-color: var(--vaadin-input-field-value-color);
  }

  ::slotted(*) {
    flex: none;
  }

  slot[name$='fix'] {
    cursor: auto;
  }

  ::slotted(:is(input, textarea))::placeholder {
    /* Use ::slotted(:is(input, textarea):placeholder-shown) to style the placeholder */
    /* because ::slotted(...)::placeholder does not work in Safari. */
    font: inherit;
    color: inherit;
  }

  ::slotted(:is(input, textarea):placeholder-shown) {
    color: var(--vaadin-input-field-placeholder-color, var(--vaadin-text-color-secondary));
  }

  :host(:focus-within) {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
    outline-offset: calc(var(--vaadin-input-field-border-width, 1px) * -1);
  }

  :host([invalid]) {
    --vaadin-input-field-border-color: var(--vaadin-input-field-error-color, var(--vaadin-text-color));
  }

  :host([readonly]) {
    border-style: dashed;
  }

  :host([readonly]:focus-within) {
    outline-style: dashed;
    --vaadin-input-field-border-color: transparent;
  }

  :host([disabled]) {
    --vaadin-input-field-value-color: var(--vaadin-input-field-disabled-text-color, var(--vaadin-text-color-disabled));
    --vaadin-input-field-background: var(
      --vaadin-input-field-disabled-background,
      var(--vaadin-background-container-strong)
    );
    --vaadin-input-field-border-color: transparent;
  }

  :host([theme~='align-start']) slot:not([name])::slotted(*) {
    text-align: start;
  }

  :host([theme~='align-center']) slot:not([name])::slotted(*) {
    text-align: center;
  }

  :host([theme~='align-end']) slot:not([name])::slotted(*) {
    text-align: end;
  }

  :host([theme~='align-left']) slot:not([name])::slotted(*) {
    text-align: left;
  }

  :host([theme~='align-right']) slot:not([name])::slotted(*) {
    text-align: right;
  }

  @media (forced-colors: active) {
    :host {
      --vaadin-input-field-background: Field;
      --vaadin-input-field-value-color: FieldText;
      --vaadin-input-field-placeholder-color: GrayText;
    }

    :host([disabled]) {
      --vaadin-input-field-value-color: GrayText;
      --vaadin-icon-color: GrayText;
    }
  }
`;var ii=class extends y(S(_(b(p)))){static get is(){return"vaadin-input-container"}static get styles(){return Fr}static get properties(){return{disabled:{type:Boolean,reflectToAttribute:!0},readonly:{type:Boolean,reflectToAttribute:!0},invalid:{type:Boolean,reflectToAttribute:!0}}}render(){return m`
      <slot name="prefix"></slot>
      <slot></slot>
      <slot name="suffix"></slot>
    `}ready(){super.ready(),this.addEventListener("pointerdown",t=>{t.target===this&&t.preventDefault()}),this.addEventListener("click",t=>{t.target===this&&this.shadowRoot.querySelector("slot:not([name])").assignedNodes({flatten:!0}).forEach(e=>e.focus&&e.focus())})}};v(ii);var zr=c`
  :host {
    align-items: center;
    border-radius: var(--vaadin-item-border-radius, var(--vaadin-radius-m));
    box-sizing: border-box;
    cursor: var(--vaadin-clickable-cursor);
    display: flex;
    column-gap: var(--vaadin-item-gap, var(--vaadin-gap-s));
    height: var(--vaadin-item-height, auto);
    padding: var(--vaadin-item-padding, var(--vaadin-padding-block-container) var(--vaadin-padding-inline-container));
    -webkit-tap-highlight-color: transparent;
  }

  :host([focus-ring]) {
    outline: var(--vaadin-focus-ring-width) solid var(--vaadin-focus-ring-color);
    outline-offset: calc(var(--vaadin-focus-ring-width) / -1);
  }

  :host([disabled]) {
    cursor: var(--vaadin-disabled-cursor);
    opacity: 0.5;
    pointer-events: var(--_vaadin-item-disabled-pointer-events, none);
  }

  :host([hidden]) {
    display: none !important;
  }

  [part='checkmark'] {
    color: var(--vaadin-item-checkmark-color, inherit);
    display: var(--vaadin-item-checkmark-display, none);
    visibility: hidden;
  }

  [part='checkmark']::before {
    content: '';
    display: block;
    background: currentColor;
    height: var(--vaadin-icon-size, 1lh);
    mask: var(--_vaadin-icon-checkmark) 50% / var(--vaadin-icon-visual-size, 100%) no-repeat;
    width: var(--vaadin-icon-size, 1lh);
  }

  :host([selected]) [part='checkmark'] {
    visibility: visible;
  }

  [part='content'] {
    flex: 1;
    display: flex;
    align-items: center;
    column-gap: inherit;
    justify-content: var(--vaadin-item-text-align, start);
  }

  @media (forced-colors: active) {
    [part='checkmark']::before {
      background: CanvasText;
    }
  }
`;var jr=r=>class extends j(U(r)){static get properties(){return{_hasVaadinItemMixin:{value:!0},selected:{type:Boolean,value:!1,reflectToAttribute:!0,observer:"_selectedChanged",sync:!0},_value:String}}get _activeKeys(){return["Enter"," "]}get value(){return this._value??this.textContent.trim()}set value(e){this._value=e}ready(){super.ready();let e=this.getAttribute("value");e!==null&&(this.value=e),this.__shouldAllowFocusWhenDisabled()&&this.style.setProperty("--_vaadin-item-disabled-pointer-events","auto")}focus(e){this.disabled&&!this.__shouldAllowFocusWhenDisabled()||super.focus(e)}_shouldSetActive(e){return!this.disabled&&!(e.type==="keydown"&&e.defaultPrevented)}_selectedChanged(e){this.setAttribute("aria-selected",e)}_disabledChanged(e){super._disabledChanged(e),e&&(this.selected=!1,this.__shouldAllowFocusWhenDisabled()||this.blur())}_onKeyDown(e){super._onKeyDown(e),this._activeKeys.includes(e.key)&&!e.defaultPrevented&&(e.preventDefault(),this.click())}__shouldAllowFocusWhenDisabled(){return!1}};var ri=class extends jr(y(S(_(b(p))))){static get is(){return"vaadin-select-item"}static get styles(){return zr}static get properties(){return{role:{type:String,value:"option",reflectToAttribute:!0}}}render(){return m`
      <span part="checkmark" aria-hidden="true"></span>
      <div part="content">
        <slot></slot>
      </div>
    `}};v(ri);function Vr(r,t){let{scrollLeft:e}=r;return t!=="rtl"?e:r.scrollWidth-r.clientWidth+e}function Ur(r,t,e){t!=="rtl"?r.scrollLeft=e:r.scrollLeft=r.clientWidth-r.scrollWidth+e}var Hr=r=>class extends z(r){get focused(){return(this._getItems()||[]).find(Re)}get _vertical(){return!0}get _tabNavigation(){return!1}focus(e){let i=this._getFocusableIndex();i>=0&&this._focus(i,e)}_getFocusableIndex(){let e=this._getItems();return Array.isArray(e)?this._getAvailableIndex(e,0,null,i=>!Q(i)):-1}_getItems(){return Array.from(this.children)}_onKeyDown(e){if(super._onKeyDown(e),e.metaKey||e.ctrlKey)return;let{key:i,shiftKey:o}=e,s=this._getItems()||[],n=s.indexOf(this.focused),a,l,f=!this._vertical&&this.getAttribute("dir")==="rtl"?-1:1;this.__isPrevKeyPressed(i,o)?(l=-f,a=n-f):this.__isNextKeyPressed(i,o)?(l=f,a=n+f):i==="Home"?(l=1,a=0):i==="End"&&(l=-1,a=s.length-1),a=this._getAvailableIndex(s,a,l,h=>!Q(h)),!(this._tabNavigation&&i==="Tab"&&(a>n&&e.shiftKey||a<n&&!e.shiftKey||a===n))&&a>=0&&(e.preventDefault(),this._focus(a,{focusVisible:!0,preventScroll:!0},!0))}__isPrevKeyPressed(e,i){return this._vertical?e==="ArrowUp":e==="ArrowLeft"||this._tabNavigation&&e==="Tab"&&i}__isNextKeyPressed(e,i){return this._vertical?e==="ArrowDown":e==="ArrowRight"||this._tabNavigation&&e==="Tab"&&!i}_focus(e,i,o=!1){let s=this._getItems();this._focusItem(s[e],i,o)}_focusItem(e,i){e&&e.focus(i)}_getAvailableIndex(e,i,o,s){let n=e.length,a=i;for(let l=0;typeof a=="number"&&l<n;l+=1,a+=o||1){a<0?a=n-1:a>=n&&(a=0);let d=e[a];if(this._isItemFocusable(d)&&this.__isMatchingItem(d,s))return a}return-1}__isMatchingItem(e,i){return typeof i=="function"?i(e):!0}_isItemFocusable(e){return!e.hasAttribute("disabled")}};var qr=r=>class extends Hr(r){static get properties(){return{disabled:{type:Boolean,value:!1,reflectToAttribute:!0},selected:{type:Number,reflectToAttribute:!0,notify:!0,sync:!0},orientation:{type:String,reflectToAttribute:!0,value:""},items:{type:Array,readOnly:!0,notify:!0},_searchBuf:{type:String,value:""}}}static get observers(){return["_enhanceItems(items, orientation, selected, disabled)"]}get _isRTL(){return!this._vertical&&this.getAttribute("dir")==="rtl"}get _scrollerElement(){return console.warn(`Please implement the '_scrollerElement' property in <${this.localName}>`),this}get _vertical(){return this.orientation!=="horizontal"}focus(e){this._observer&&this._observer.flush();let i=Array.isArray(this.items)?this.items:[],o=this._getAvailableIndex(i,0,null,s=>s.tabIndex===0&&!Q(s));o>=0?this._focus(o,e):super.focus(e)}ready(){super.ready(),this.addEventListener("click",i=>this._onClick(i));let e=this.shadowRoot.querySelector("slot:not([name])");this._observer=new R(e,()=>{this._setItems(this._filterItems([...this.children]))})}_getItems(){return this.items}_enhanceItems(e,i,o,s){if(!s&&e){this.setAttribute("aria-orientation",i||"vertical"),e.forEach(a=>{i?a.setAttribute("orientation",i):a.removeAttribute("orientation")}),this._setFocusable(o<0||!o?0:o);let n=e[o];e.forEach(a=>{a.selected=a===n}),n&&!n.disabled&&this._scrollToItem(o)}}_filterItems(e){return e.filter(i=>i._hasVaadinItemMixin)}_onClick(e){if(e.metaKey||e.shiftKey||e.ctrlKey||e.defaultPrevented)return;let i=this._filterItems(e.composedPath())[0],o;i&&!i.disabled&&(o=this.items.indexOf(i))>=0&&(this.selected=o)}_searchKey(e,i){this._searchReset=D.debounce(this._searchReset,Ni.after(500),()=>{this._searchBuf=""}),this._searchBuf+=i.toLowerCase(),this.items.some(s=>this.__isMatchingKey(s))||(this._searchBuf=i.toLowerCase());let o=this._searchBuf.length===1?e+1:e;return this._getAvailableIndex(this.items,o,1,s=>this.__isMatchingKey(s)&&getComputedStyle(s).display!=="none")}__isMatchingKey(e){return e.textContent.replace(/[^\p{L}\p{Nd}]/gu,"").toLowerCase().startsWith(this._searchBuf)}_onKeyDown(e){if(e.metaKey||e.ctrlKey)return;let i=e.key,o=this.items.indexOf(this.focused);if(/[\p{L}\p{Nd}]/u.test(i)&&i.length===1){let s=this._searchKey(o,i);s>=0&&this._focus(s);return}super._onKeyDown(e)}_setFocusable(e){e=this._getAvailableIndex(this.items,e,1);let i=this.items[e];this.items.forEach(o=>{o.tabIndex=o===i?0:-1})}_focus(e,i){this.items.forEach((o,s)=>{o.focused=s===e}),this._setFocusable(e),this._scrollToItem(e),super._focus(e,i??{preventScroll:!0})}_scrollToItem(e){let i=this._getItems()[e];i&&i.scrollIntoView({block:"nearest",inline:"nearest"})}_scroll(e){if(this._vertical)this._scrollerElement.scrollTop+=e;else{let i=this.getAttribute("dir")||"ltr",o=Vr(this._scrollerElement,i)+e;Ur(this._scrollerElement,i,o)}}_isItemFocusable(e){return e.disabled&&e.__shouldAllowFocusWhenDisabled?e.__shouldAllowFocusWhenDisabled():super._isItemFocusable(e)}};var Kr=c`
  :host {
    --vaadin-item-checkmark-display: block;
    display: flex;
  }

  :host([hidden]) {
    display: none !important;
  }

  [part='items'] {
    height: 100%;
    overflow-y: auto;
    width: 100%;
  }

  [part='items'] ::slotted(hr) {
    border-color: var(--vaadin-divider-color, var(--vaadin-border-color-secondary));
    border-width: 0 0 1px;
    margin: 4px 8px;
    margin-inline-start: calc(var(--vaadin-icon-size, 1lh) + var(--vaadin-item-gap, var(--vaadin-gap-s)) + 8px);
  }
`;var oi=class extends qr(y(S(_(b(p))))){static get is(){return"vaadin-select-list-box"}static get styles(){return Kr}static get properties(){return{orientation:{readOnly:!0}}}get _scrollerElement(){return this.shadowRoot.querySelector('[part="items"]')}render(){return m`
      <div part="items">
        <slot></slot>
      </div>
    `}ready(){super.ready(),this.setAttribute("role","listbox")}};v(oi);var Wr=c`
  :host {
    align-items: flex-start;
    justify-content: flex-start;
  }

  [part='overlay'] {
    min-width: var(--vaadin-select-overlay-width, var(--_vaadin-select-overlay-default-width));
  }

  [part='content'] {
    padding: var(--vaadin-item-overlay-padding, 4px);
  }

  [part='backdrop'] {
    background: transparent;
  }
`;var si={start:"top",end:"bottom"},ni={start:"left",end:"right"},Gr=new ResizeObserver(r=>{setTimeout(()=>{r.forEach(t=>{t.target.__overlay&&t.target.__overlay._updatePosition()})})}),Xr=r=>class extends r{static get properties(){return{positionTarget:{type:Object,value:null,sync:!0},horizontalAlign:{type:String,value:"start",sync:!0},verticalAlign:{type:String,value:"top",sync:!0},noHorizontalOverlap:{type:Boolean,value:!1,sync:!0},noVerticalOverlap:{type:Boolean,value:!1,sync:!0},requiredVerticalSpace:{type:Number,value:0,sync:!0}}}constructor(){super(),this._hasOverlayPositionMixin=!0,this.__onScroll=this.__onScroll.bind(this),this._updatePosition=this._updatePosition.bind(this)}connectedCallback(){super.connectedCallback(),this.opened&&this.__addUpdatePositionEventListeners()}disconnectedCallback(){super.disconnectedCallback(),this.__removeUpdatePositionEventListeners()}updated(e){if(super.updated(e),e.has("positionTarget")){let o=e.get("positionTarget");this.__oldContentWidth=void 0,this.__oldContentHeight=void 0,(!this.positionTarget&&o||this.positionTarget&&!o&&this.__margins)&&this.__resetPosition()}(e.has("opened")||e.has("positionTarget"))&&this.__updatePositionSettings(this.opened,this.positionTarget),["horizontalAlign","verticalAlign","noHorizontalOverlap","noVerticalOverlap","requiredVerticalSpace"].some(o=>e.has(o))&&this._updatePosition()}__addUpdatePositionEventListeners(){window.visualViewport.addEventListener("resize",this._updatePosition),window.visualViewport.addEventListener("scroll",this.__onScroll,!0),this.__positionTargetAncestorRootNodes=qi(this.positionTarget),this.__positionTargetAncestorRootNodes.forEach(e=>{e.addEventListener("scroll",this.__onScroll,!0)}),this.positionTarget&&(this.__observePositionTargetMove=Sr(this.positionTarget,()=>{this._updatePosition()}))}__removeUpdatePositionEventListeners(){window.visualViewport.removeEventListener("resize",this._updatePosition),window.visualViewport.removeEventListener("scroll",this.__onScroll,!0),this.__positionTargetAncestorRootNodes&&(this.__positionTargetAncestorRootNodes.forEach(e=>{e.removeEventListener("scroll",this.__onScroll,!0)}),this.__positionTargetAncestorRootNodes=null),this.__observePositionTargetMove&&(this.__observePositionTargetMove(),this.__observePositionTargetMove=null)}__updatePositionSettings(e,i){if(this.__removeUpdatePositionEventListeners(),i&&(i.__overlay=null,Gr.unobserve(i),e&&(this.__addUpdatePositionEventListeners(),i.__overlay=this,Gr.observe(i))),e){let o=getComputedStyle(this);this.__margins||(this.__margins={},["top","bottom","left","right"].forEach(s=>{this.__margins[s]=parseInt(o[s],10)})),this._updatePosition(),requestAnimationFrame(()=>this._updatePosition())}}__onScroll(e){e.target instanceof Node&&this._deepContains(e.target)||this._updatePosition()}__resetPosition(){this.__margins=null,Object.assign(this.style,{justifyContent:"",alignItems:"",top:"",bottom:"",left:"",right:""}),k(this,"bottom-aligned",!1),k(this,"top-aligned",!1),k(this,"end-aligned",!1),k(this,"start-aligned",!1)}_updatePosition(){if(!this.positionTarget||!this.opened||!this.__margins)return;let e=this.positionTarget.getBoundingClientRect();if(e.width===0&&e.height===0&&this.opened){this.opened=!1;return}let i=this.__shouldAlignStartVertically(e);this.style.justifyContent=i?"flex-start":"flex-end";let o=this.__isRTL,s=this.__shouldAlignStartHorizontally(e,o),n=!o&&s||o&&!s;this.style.alignItems=n?"flex-start":"flex-end";let a=this.getBoundingClientRect(),l=this.__calculatePositionInOneDimension(e,a,this.noVerticalOverlap,si,this,i),d=this.__calculatePositionInOneDimension(e,a,this.noHorizontalOverlap,ni,this,s);Object.assign(this.style,l,d),k(this,"bottom-aligned",!i),k(this,"top-aligned",i),k(this,"end-aligned",!n),k(this,"start-aligned",n)}__shouldAlignStartHorizontally(e,i){let o=Math.max(this.__oldContentWidth||0,this.$.overlay.offsetWidth);this.__oldContentWidth=this.$.overlay.offsetWidth;let s=Math.min(window.innerWidth,document.documentElement.clientWidth),n=!i&&this.horizontalAlign==="start"||i&&this.horizontalAlign==="end";return this.__shouldAlignStart(e,o,s,this.__margins,n,this.noHorizontalOverlap,ni)}__shouldAlignStartVertically(e){let i=this.requiredVerticalSpace||Math.max(this.__oldContentHeight||0,this.$.overlay.offsetHeight);this.__oldContentHeight=this.$.overlay.offsetHeight;let o=Math.min(window.innerHeight,document.documentElement.clientHeight),s=this.verticalAlign==="top";return this.__shouldAlignStart(e,i,o,this.__margins,s,this.noVerticalOverlap,si)}__shouldAlignStart(e,i,o,s,n,a,l){let d=o-e[a?l.end:l.start]-s[l.end],f=e[a?l.start:l.end]-s[l.start],h=n?d:f,C=h>(n?f:d)||h>i;return n===C}__adjustBottomProperty(e,i,o){let s;if(e===i.end){if(i.end===si.end){let n=Math.min(window.innerHeight,document.documentElement.clientHeight);if(o>n&&this.__oldViewportHeight){let a=this.__oldViewportHeight-n;s=o-a}this.__oldViewportHeight=n}if(i.end===ni.end){let n=Math.min(window.innerWidth,document.documentElement.clientWidth);if(o>n&&this.__oldViewportWidth){let a=this.__oldViewportWidth-n;s=o-a}this.__oldViewportWidth=n}}return s}__calculatePositionInOneDimension(e,i,o,s,n,a){let l=a?s.start:s.end,d=a?s.end:s.start,f=parseFloat(n.style[l]||getComputedStyle(n)[l]),h=this.__adjustBottomProperty(l,s,f),w=i[a?s.start:s.end]-e[o===a?s.end:s.start],C=h?`${h}px`:`${f+w*(a?-1:1)}px`;return{[l]:C,[d]:""}}};var Yr=r=>class extends Xr(et(S(r))){static get observers(){return["_updateOverlayWidth(opened, positionTarget)"]}ready(){super.ready(),this.restoreFocusOnClose=!0}get _contentRoot(){return this._rendererRoot}get _rendererRoot(){if(!this.__savedRoot){let e=document.createElement("div");e.setAttribute("slot","overlay"),this.owner.appendChild(e),this.__savedRoot=e}return this.__savedRoot}_shouldCloseOnOutsideClick(e){return!0}_mouseDownListener(e){super._mouseDownListener(e),e.preventDefault()}_getMenuElement(){return Array.from(this._rendererRoot.children).find(e=>e.localName!=="style")}_updateOverlayWidth(e,i){e&&i&&this.style.setProperty("--_vaadin-select-overlay-default-width",`${i.offsetWidth}px`)}requestContentUpdate(){if(super.requestContentUpdate(),this.owner){let e=this._getMenuElement();this.owner._assignMenuElement(e)}}};var ai=class extends Yr(y(_(b(p)))){static get is(){return"vaadin-select-overlay"}static get styles(){return[xe,Wr]}render(){return m`
      <div id="backdrop" part="backdrop" ?hidden="${!this.withBackdrop}"></div>
      <div part="overlay" id="overlay">
        <div part="content" id="content">
          <slot></slot>
        </div>
      </div>
    `}updated(t){super.updated(t),t.has("renderer")&&this.requestContentUpdate()}};v(ai);var Zr=c`
  :host {
    min-height: 1lh;
    outline: none;
    overflow: hidden;
    white-space: nowrap;
    width: 100%;
    display: flex;
    align-items: center;
  }

  ::slotted(*) {
    padding: 0;
    cursor: inherit;
  }

  .vaadin-button-container,
  [part='label'] {
    display: contents;
  }

  :host([placeholder]) {
    color: var(--vaadin-input-field-placeholder-color, var(--vaadin-text-color-secondary));
  }

  :host([disabled]) {
    pointer-events: none;
  }
`;var li=class extends Fe(y(_(b(p)))){static get is(){return"vaadin-select-value-button"}static get styles(){return Zr}render(){return m`
      <div class="vaadin-button-container">
        <span part="label">
          <slot></slot>
        </span>
      </div>
    `}};v(li);var Jr=c`
  .sr-only {
    border: 0 !important;
    clip: rect(1px, 1px, 1px, 1px) !important;
    clip-path: inset(50%) !important;
    height: 1px !important;
    margin: -1px !important;
    overflow: hidden !important;
    padding: 0 !important;
    position: absolute !important;
    width: 1px !important;
    white-space: nowrap !important;
  }
`;var Qr=c`
  [part$='button'] {
    color: var(--vaadin-input-field-button-text-color, var(--vaadin-text-color-secondary));
    cursor: var(--vaadin-clickable-cursor);
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none;
    user-select: none;
    /* Ensure minimum click target (WCAG) */
    padding: max(0px, (24px - var(--vaadin-icon-size, 1lh)) / 2);
    margin: min(0px, (24px - var(--vaadin-icon-size, 1lh)) / -2);
  }

  /* Icon */
  [part$='button']::before {
    background: currentColor;
    content: '';
    display: block;
    height: var(--vaadin-icon-size, 1lh);
    width: var(--vaadin-icon-size, 1lh);
    mask-size: var(--vaadin-icon-visual-size, 100%);
    mask-position: 50%;
    mask-repeat: no-repeat;
  }

  :host(:is(:not([clear-button-visible][has-value]), [disabled], [readonly])) [part~='clear-button'] {
    display: none;
  }

  [part~='clear-button']::before {
    mask-image: var(--_vaadin-icon-cross);
  }

  :host(:is([readonly], [disabled])) [part$='button'] {
    color: var(--vaadin-text-color-disabled);
    cursor: var(--vaadin-disabled-cursor);
  }

  @media (forced-colors: active) {
    [part$='button']::before {
      background: CanvasText;
    }

    :host([disabled]) [part$='button'] {
      color: GrayText;
    }

    :host([disabled]) [part$='button']::before {
      background: GrayText;
    }
  }
`;var eo=[H,Qr];var to=c`
  :host {
    position: relative;
  }

  ::slotted([slot='value']) {
    flex: 1;
  }

  ::slotted(div[slot='overlay']) {
    display: contents;
  }

  :host(:not([focus-ring])) [part='input-field'] {
    outline: none;
  }

  :host([readonly]:not([focus-ring])) [part='input-field'] {
    --vaadin-input-field-border-color: inherit;
  }

  [part='input-field'],
  :host(:not([readonly])) ::slotted([slot='value']) {
    cursor: var(--vaadin-clickable-cursor);
  }

  [part~='toggle-button']::before {
    mask-image: var(--_vaadin-icon-chevron-down);
  }

  :host([readonly]) [part~='toggle-button'] {
    display: none;
  }

  :host([theme~='align-start']) {
    --vaadin-item-text-align: start;
  }

  :host([theme~='align-center']) {
    --vaadin-item-text-align: center;
  }

  :host([theme~='align-end']) {
    --vaadin-item-text-align: end;
  }

  :host([theme~='align-left']) {
    --vaadin-item-text-align: left;
  }

  :host([theme~='align-right']) {
    --vaadin-item-text-align: right;
  }

  :host([theme~='align-start']) ::slotted([slot='value']) {
    justify-content: start;
  }

  :host([theme~='align-center']) ::slotted([slot='value']) {
    justify-content: center;
  }

  :host([theme~='align-end']) ::slotted([slot='value']) {
    justify-content: end;
  }

  :host([theme~='align-left']) ::slotted([slot='value']) {
    justify-content: left;
  }

  :host([theme~='align-right']) ::slotted([slot='value']) {
    justify-content: right;
  }
`;var tt=class{constructor(t,e){this.query=t,this.callback=e,this._boundQueryHandler=this._queryHandler.bind(this)}hostConnected(){this._removeListener(),this._mediaQuery=window.matchMedia(this.query),this._addListener(),this._queryHandler(this._mediaQuery)}hostDisconnected(){this._removeListener()}_addListener(){this._mediaQuery&&this._mediaQuery.addListener(this._boundQueryHandler)}_removeListener(){this._mediaQuery&&this._mediaQuery.removeListener(this._boundQueryHandler),this._mediaQuery=null}_queryHandler(t){typeof this.callback=="function"&&this.callback(t.matches)}};var it=class extends A{constructor(t){super(t,"value","vaadin-select-value-button",{initializer:(e,i)=>{i._setFocusElement(e),i.ariaTarget=e,i.stateTarget=e,e.setAttribute("aria-haspopup","listbox")}})}};var io=r=>class extends ae(Ve(z(de(r)))){static get properties(){return{items:{type:Array,observer:"__itemsChanged"},opened:{type:Boolean,value:!1,notify:!0,observer:"_openedChanged",reflectToAttribute:!0,sync:!0},renderer:{type:Object},value:{type:String,value:"",notify:!0,observer:"_valueChanged",sync:!0},name:{type:String},placeholder:{type:String},readonly:{type:Boolean,value:!1,reflectToAttribute:!0},noVerticalOverlap:{type:Boolean,value:!1},_phone:Boolean,_phoneMediaQuery:{value:"(max-width: 450px), (max-height: 450px)"},_inputContainer:Object,_items:Object}}static get delegateAttrs(){return[...super.delegateAttrs,"invalid"]}static get observers(){return["_updateAriaExpanded(opened, focusElement)","_updateSelectedItem(value, _items, placeholder, focusElement)"]}constructor(){super(),this._itemId=`value-${this.localName}-${se()}`,this._srLabelController=new le(this),this._srLabelController.slotName="sr-label"}disconnectedCallback(){super.disconnectedCallback(),this.opened=!1}ready(){super.ready(),this._inputContainer=this.shadowRoot.querySelector('[part~="input-field"]'),this._overlayElement=this.$.overlay,this._valueButtonController=new it(this),this.addController(this._valueButtonController),this.addController(this._srLabelController),this.addController(new tt(this._phoneMediaQuery,e=>{this._phone=e})),this._tooltipController=new I(this),this._tooltipController.setPosition("top"),this._tooltipController.setAriaTarget(this.focusElement),this.addController(this._tooltipController)}updated(e){super.updated(e),e.has("_phone")&&this.toggleAttribute("phone",this._phone)}requestContentUpdate(){this._overlayElement&&this._overlayElement.requestContentUpdate()}_requiredChanged(e){super._requiredChanged(e),e===!1&&this._requestValidation()}__itemsChanged(e,i){(e||i)&&this.requestContentUpdate()}_assignMenuElement(e){e&&e!==this.__lastMenuElement&&(this._menuElement=e,this.__initMenuItems(e),e.addEventListener("items-changed",()=>{this.__initMenuItems(e)}),e.addEventListener("selected-changed",()=>this.__updateValueButton()),e.addEventListener("keydown",i=>this._onKeyDownInside(i),!0),e.addEventListener("click",i=>{let o=i.composedPath().find(s=>s._hasVaadinItemMixin);this.__dispatchChangePending=o?.value!==void 0&&o.value!==this.value,this.opened=!1},!0),this.__lastMenuElement=e),this._menuElement&&this._menuElement.items&&this._updateSelectedItem(this.value,this._menuElement.items)}__initMenuItems(e){e.items&&(this._items=e.items)}_valueChanged(e,i){this.toggleAttribute("has-value",!!e),i!==void 0&&!this.__dispatchChangePending&&this._requestValidation()}_onClick(e){this.disabled||(e.preventDefault(),this.opened=!this.readonly)}_onEscape(e){this.opened&&(e.stopPropagation(),this.opened=!1)}_onToggleMouseDown(e){e.preventDefault(),this.opened||this.focusElement.focus()}_onKeyDown(e){if(super._onKeyDown(e),!(e.altKey||e.shiftKey||e.ctrlKey||e.metaKey)&&e.target===this.focusElement&&!this.readonly&&!this.disabled&&!this.opened){if(/^(Enter|SpaceBar|\s|ArrowDown|Down|ArrowUp|Up)$/u.test(e.key))e.preventDefault(),this.opened=!0;else if(/[\p{L}\p{Nd}]/u.test(e.key)&&e.key.length===1){let o=this._menuElement.selected??-1,s=this._menuElement._searchKey(o,e.key);s>=0&&(this.__dispatchChangePending=!0,this._updateAriaLive(!0),this._menuElement.selected=s)}}}_onKeyDownInside(e){e.key==="Tab"&&(this.focusElement.setAttribute("tabindex","-1"),this._overlayElement.restoreFocusOnClose=!1,this.opened=!1,setTimeout(()=>{this.focusElement.setAttribute("tabindex","0"),this._overlayElement.restoreFocusOnClose=!0}))}_openedChanged(e,i){if(e){if(this.disabled||this.readonly){this.opened=!1;return}this._updateAriaLive(!1);let o=this.hasAttribute("focus-ring");this._openedWithFocusRing=o,o&&this.removeAttribute("focus-ring")}else i&&(this._openedWithFocusRing&&this.setAttribute("focus-ring",""),!this.__dispatchChangePending&&!this._keyboardActive&&this._requestValidation())}_updateAriaExpanded(e,i){i&&i.setAttribute("aria-expanded",e?"true":"false")}_updateAriaLive(e){this.focusElement&&(e?this.focusElement.setAttribute("aria-live","polite"):this.focusElement.removeAttribute("aria-live"))}__attachSelectedItem(e){let i,o=e.getAttribute("label");o?i=this.__createItemElement({label:o}):i=e.cloneNode(!0),i._sourceItem=e,this.__appendValueItemElement(i,this.focusElement),i.selected=!0}__createItemElement(e){let i=document.createElement(e.component||"vaadin-select-item");return e.label&&(i.textContent=e.label),e.value&&(i.value=e.value),e.disabled&&(i.disabled=e.disabled),e.className&&(i.className=e.className),i}__appendValueItemElement(e,i){i.appendChild(e),e.removeAttribute("tabindex"),e.removeAttribute("aria-selected"),e.removeAttribute("role"),e.removeAttribute("focused"),e.removeAttribute("focus-ring"),e.removeAttribute("active"),e.setAttribute("id",this._itemId)}_accessibleNameChanged(e){this._srLabelController.setLabel(e),this._setCustomAriaLabelledBy(e?this._srLabelController.defaultId:null)}_accessibleNameRefChanged(e){this._setCustomAriaLabelledBy(e)}_setCustomAriaLabelledBy(e){let i=this._getLabelIdWithItemId(e);this._fieldAriaController.setLabelId(i,!0)}_getLabelIdWithItemId(e){let o=(this._items?this._items[this._menuElement.selected]:!1)||this.placeholder?this._itemId:"";return e?`${e} ${o}`.trim():null}__updateValueButton(){let e=this.focusElement;if(!e)return;e.innerHTML="";let i=this._items?this._items[this._menuElement.selected]:void 0;if(e.removeAttribute("placeholder"),this._hasContent(i))this.__attachSelectedItem(i);else if(this.placeholder){let s=this.__createItemElement({label:this.placeholder});this.__appendValueItemElement(s,e),e.setAttribute("placeholder","")}!this._valueChanging&&i&&(this._selectedChanging=!0,this.value=i.value||"",this.__dispatchChangePending&&this.__dispatchChange(),delete this._selectedChanging);let o=i||this.placeholder?{newId:this._itemId}:{oldId:this._itemId};q(e,"aria-labelledby",o),(this.accessibleName||this.accessibleNameRef)&&this._setCustomAriaLabelledBy(this.accessibleNameRef||this._srLabelController.defaultId)}_hasContent(e){if(!e)return!1;let i=!!(e.hasAttribute("label")?e.getAttribute("label"):e.textContent.trim()),o=e.childElementCount>0;return i||o}_updateSelectedItem(e,i,o){if(i){let s=e==null?e:e.toString();this._menuElement.selected=i.reduce((n,a,l)=>n===void 0&&a.value===s?l:n,void 0),this._selectedChanging||(this._valueChanging=!0,this.__updateValueButton(),delete this._valueChanging)}else o&&this.__updateValueButton()}_shouldRemoveFocus(e){return!this.contains(e.relatedTarget)}_setFocused(e){super._setFocused(e),!e&&document.hasFocus()&&this._requestValidation()}checkValidity(){return!this.required||this.readonly||!!this.value}__defaultRenderer(e,i){if(!this.items||this.items.length===0){e.textContent="";return}let o=e.firstElementChild;o||(o=document.createElement("vaadin-select-list-box"),e.appendChild(o)),o.textContent="",this.items.forEach(s=>{o.appendChild(this.__createItemElement(s))})}__dispatchChange(){this._requestValidation(),this.dispatchEvent(new CustomEvent("change",{bubbles:!0})),this.__dispatchChangePending=!1}};var di=class extends io(E(y(_(b(p))))){static get is(){return"vaadin-select"}static get styles(){return[eo,Jr,to]}render(){return m`
      <div class="vaadin-select-container">
        <div part="label" @click="${this._onClick}">
          <slot name="label"></slot>
          <span part="required-indicator" aria-hidden="true" @click="${this.focus}"></span>
        </div>

        <vaadin-input-container
          part="input-field"
          .readonly="${this.readonly}"
          .disabled="${this.disabled}"
          .invalid="${this.invalid}"
          theme="${we(this._theme)}"
          @click="${this._onClick}"
        >
          <slot name="prefix" slot="prefix"></slot>
          <slot name="value"></slot>
          <div
            part="field-button toggle-button"
            slot="suffix"
            aria-hidden="true"
            @mousedown="${this._onToggleMouseDown}"
          ></div>
        </vaadin-input-container>

        <div part="helper-text">
          <slot name="helper"></slot>
        </div>

        <div part="error-message">
          <slot name="error-message"></slot>
        </div>
      </div>

      <vaadin-select-overlay
        id="overlay"
        .owner="${this}"
        .positionTarget="${this._inputContainer}"
        .opened="${this.opened}"
        .withBackdrop="${this._phone}"
        .renderer="${this.renderer||this.__defaultRenderer}"
        ?phone="${this._phone}"
        theme="${we(this._theme)}"
        ?no-vertical-overlap="${this.noVerticalOverlap}"
        exportparts="backdrop, overlay, content"
        @opened-changed="${this._onOpenedChanged}"
        @vaadin-overlay-open="${this._onOverlayOpen}"
      >
        <slot name="overlay"></slot>
      </vaadin-select-overlay>

      <slot name="tooltip"></slot>
      <div class="sr-only">
        <slot name="sr-label"></slot>
      </div>
    `}_onOpenedChanged(t){this.opened=t.detail.value}_onOverlayOpen(){this._menuElement&&this._menuElement.focus({focusVisible:V()})}};v(di);var Ps="post",T=window.__HA_OPS_TEXT__||{},Ds=new Set(["preview","save_preview","apply","save","reset_git_state","disk_usage","deleted_devices_preview","retained_devices_preview","retained_devices_delete","internal_ids_preview","internal_ids_migrate","deleted_devices_delete","deleted_devices_confirm","deleted_devices_revert","rollback"]);function Rs(){if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();let r=new Uint8Array(16);if(globalThis.crypto?.getRandomValues)globalThis.crypto.getRandomValues(r);else for(let e=0;e<r.length;e+=1)r[e]=Math.floor(Math.random()*256);r[6]=r[6]&15|64,r[8]=r[8]&63|128;let t=Array.from(r,e=>e.toString(16).padStart(2,"0")).join("");return`${t.slice(0,8)}-${t.slice(8,12)}-${t.slice(12,16)}-${t.slice(16,20)}-${t.slice(20)}`}function ro(){let r=new URL(window.location.href);if(!r.pathname.endsWith("/")){let t=r.pathname.lastIndexOf("/"),e=r.pathname.slice(t+1);r.pathname=e&&!e.includes(".")?`${r.pathname}/`:r.pathname.slice(0,t+1)}return r}function Bs(){let r=new URL("ws",ro());return r.protocol=window.location.protocol==="https:"?"wss:":"ws:",r.href}function Fs(r){return(new URL(r,window.location.href).pathname.split("/").filter(Boolean).pop()||"").replaceAll("-","_")}function zs(r){let t={};for(let[e,i]of new FormData(r).entries())Object.hasOwn(t,e)?t[e]=Array.isArray(t[e])?[...t[e],i]:[t[e],i]:t[e]=i;return t}var ci=class extends p{static properties={lines:{type:Array},status:{type:String}};static styles=c`
    :host { display: contents; }
    pre { box-sizing: border-box; height: 100%; margin: 0; overflow: auto; white-space: pre-wrap; }
  `;constructor(){super(),this.lines=[],this.status="idle"}render(){return m`<pre data-testid="operation-log" aria-label="Operation log">${this.lines.join(`
`)}</pre>`}firstUpdated(){let t=this.renderRoot.querySelector("pre"),e=null;try{e=JSON.parse(sessionStorage.getItem("haOpsLogScrollState")||"null")}catch{}requestAnimationFrame(()=>{t.scrollTop=e?.sticky===!1?Math.min(e.scrollTop||0,t.scrollHeight-t.clientHeight):t.scrollHeight}),t.addEventListener("scroll",()=>{let i=t.scrollHeight-t.scrollTop-t.clientHeight<=4;sessionStorage.setItem("haOpsLogScrollState",JSON.stringify({sticky:i,scrollTop:t.scrollTop}))},{passive:!0})}updated(){let t=this.renderRoot.querySelector("pre"),e=null;try{e=JSON.parse(sessionStorage.getItem("haOpsLogScrollState")||"null")}catch{}(!e||e.sticky!==!1)&&requestAnimationFrame(()=>{t.scrollTop=t.scrollHeight})}};customElements.define("ha-ops-log",ci);var hi=class extends p{static properties={path:{type:String},cursor:{type:Object},generation:{type:Number},expanded:{type:Boolean},diff:{type:String},diffState:{type:String}};static styles=c`
    :host { display: block; border: 1px solid var(--ha-border); border-radius: 8px; overflow: hidden; }
    header { display: flex; align-items: center; gap: .75rem; padding: .65rem .75rem; }
    code { min-width: 0; overflow-wrap: anywhere; }
    pre { margin: 0; padding: .75rem; overflow: auto; white-space: pre; border-top: 1px solid var(--ha-border); }
    [role="status"] { padding: .75rem; color: var(--ha-muted); }
  `;constructor(){super(),this.path="",this.cursor=null,this.generation=0,this.expanded=!1,this.diff="",this.diffState="idle"}willUpdate(t){(t.has("cursor")||t.has("generation"))&&(this.expanded=!1,this.diff="",this.diffState="idle")}render(){return m`
      <header>
        <vaadin-button theme="secondary" aria-expanded=${String(this.expanded)} @click=${()=>this.setExpanded(!this.expanded)}>
          ${this.expanded?T.collapse:T.expand}
        </vaadin-button>
        <code>${this.path}</code>
      </header>
      ${this.expanded?this.diffState==="loaded"?m`<pre aria-label="Diff detail">${this.diff}</pre>`:m`<div role="status">${this.diffState==="stale"?T.unavailableDiff:T.loadingDiff}</div>`:g}
    `}async setExpanded(t){if(this.expanded=t,!(!t||this.diffState==="loaded")){this.diffState="loading";try{let i=await(await fetch(`diff-get?cursor=${encodeURIComponent(JSON.stringify(this.cursor))}&path=${encodeURIComponent(this.path)}`)).json();if(!i.ok||Number(this.cursor?.generation)!==Number(this.generation))throw new Error("stale");this.diff=i.diff,this.diffState="loaded"}catch{this.diff="",this.diffState="stale"}}}};customElements.define("ha-ops-preview-file",hi);var ui=class extends p{static properties={state:{type:Object},direction:{type:String},running:{type:Boolean}};static styles=c`
    :host { display: grid; gap: .65rem; margin-top: 1rem; }
    header { display: flex; align-items: center; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .actions { display: flex; gap: .5rem; }
    .files { display: grid; gap: .5rem; }
    footer { display: flex; justify-content: flex-end; }
  `;constructor(){super(),this.state={},this.direction="apply",this.running=!1}get paths(){return this.direction==="save"?this.state.last_save_preview_paths||[]:this.state.last_preview_paths||[]}get cursor(){return this.direction==="save"?this.state.last_save_diff_cursor:this.state.last_diff_cursor}get finalCommand(){return this.direction==="save"?"save":"apply"}get finalLabel(){return this.direction==="save"?T.save:T.apply}render(){return this.paths.length?m`
      <header>
        <h3>${this.direction==="save"?T.savePreview:T.applyPreview}</h3>
        <div class="actions">
          <vaadin-button theme="secondary" @click=${()=>this.setAll(!0)}>${T.expandAll}</vaadin-button>
          <vaadin-button theme="secondary" @click=${()=>this.setAll(!1)}>${T.collapseAll}</vaadin-button>
        </div>
      </header>
      <div class="files">
        ${this.paths.map(t=>m`<ha-ops-preview-file
          data-testid="preview-file" .path=${t} .cursor=${this.cursor}
          .generation=${Number(this.state.operation_generation||0)}></ha-ops-preview-file>`)}
      </div>
      <footer>
        <vaadin-button theme="primary" ?disabled=${this.running} @click=${()=>this.runFinalAction()}>
          ${this.finalLabel}
        </vaadin-button>
      </footer>
    `:g}setAll(t){for(let e of this.renderRoot.querySelectorAll("ha-ops-preview-file"))e.setExpanded(t)}runFinalAction(){this.running||this.dispatchEvent(new CustomEvent("ha-ops-command",{bubbles:!0,composed:!0,detail:{command:this.finalCommand,payload:{}}}))}};customElements.define("ha-ops-preview",ui);var pi=class extends p{static properties={connection:{type:String},revision:{type:Number},state:{type:Object},confirmOpen:{type:Boolean},confirmMessage:{type:String}};static styles=c`
    :host { display: contents; }
    .connection { position: fixed; right: .75rem; bottom: .75rem; z-index: 20; padding: .35rem .6rem;
      border: 1px solid var(--ha-ops-muted-border, #9aa0a6); border-radius: 999px;
      color: var(--ha-ops-muted-text, #5f6368); background: var(--ha-ops-surface, #fff); font: 12px/1.2 system-ui; }
  `;constructor(){super(),this.connection="connecting",this.revision=0,this.state={},this.confirmOpen=!1,this.confirmMessage="",this.confirmForm=null,this.socket=null,this.pending=new Map,this.nextRequestId=1,this.reconnectTimer=null,this.replayPending=!0,this.queuedFrames=[]}connectedCallback(){super.connectedCallback(),this.addEventListener("submit",this.onSubmit),this.upgradeControls(),this.observeLayout(),this.connect(),window.__HA_OPS_ENABLE_TEST_HOOKS__===!0&&(window.__haOpsTestCloseWs=()=>this.socket?.close())}disconnectedCallback(){this.removeEventListener("submit",this.onSubmit),this.reconnectTimer&&clearTimeout(this.reconnectTimer),this.socket&&this.socket.close(),super.disconnectedCallback()}render(){return m`
      <slot></slot>
      <section data-testid="reactive-previews">
        ${this.state.last_preview_paths?.length?m`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="apply"
              @ha-ops-command=${this.onCommand}></ha-ops-preview>`:g}
        ${this.state.last_save_preview_paths?.length?m`<ha-ops-preview data-testid="preview" .state=${this.state} .running=${this.isRunning()} direction="save"
              @ha-ops-command=${this.onCommand}></ha-ops-preview>`:g}
      </section>
      <vaadin-confirm-dialog
        .opened=${this.confirmOpen}
        .message=${this.confirmMessage}
        .confirmText=${T.confirm}
        cancel-button-visible
        @confirm=${this.confirmMutation}
        @cancel=${()=>{this.confirmOpen=!1,this.confirmForm=null}}
      ></vaadin-confirm-dialog>
      <span class="connection" role="status" data-testid="connection-status">${this.connection}</span>
    `}upgradeControls(){for(let t of this.querySelectorAll("button:not([data-vaadin-upgraded])")){let e=document.createElement("vaadin-button");e.textContent=t.textContent,e.disabled=t.disabled,e.className=t.className,t.disabled&&e.setAttribute("data-server-disabled","true"),e.setAttribute("data-vaadin-upgraded","true"),e.setAttribute("role","button"),t.classList.contains("secondary")?e.setAttribute("theme","secondary"):e.setAttribute("theme","primary");for(let i of t.attributes)["class","type","disabled"].includes(i.name)||e.setAttribute(i.name,i.value);e.addEventListener("click",()=>{e.disabled||(t.type==="submit"?e.closest("form")?.requestSubmit():this.handleButton(e))}),t.replaceWith(e)}for(let t of this.querySelectorAll('input[type="checkbox"]:not([data-vaadin-upgraded])')){let e=document.createElement("vaadin-checkbox");e.name=t.name,e.value=t.value,e.checked=t.checked,e.disabled=t.disabled,e.setAttribute("data-vaadin-upgraded","true"),e.setAttribute("aria-label",t.closest("label")?.innerText.trim()||t.name||"Selection"),t.disabled&&e.setAttribute("data-server-disabled","true"),e.addEventListener("change",()=>{t.checked=e.checked;let i=e.closest("form[data-auto-submit='change']");i&&i.requestSubmit()}),t.replaceWith(e)}for(let t of this.querySelectorAll("select:not([data-vaadin-upgraded])")){let e=document.createElement("vaadin-select");e.name=t.name,e.value=t.value,e.items=Array.from(t.options).map(i=>({label:i.textContent,value:i.value})),e.disabled=t.disabled,e.setAttribute("data-vaadin-upgraded","true"),e.setAttribute("aria-label",t.closest("label")?.innerText.trim()||t.name||"Selection"),t.disabled&&e.setAttribute("data-server-disabled","true"),e.addEventListener("change",()=>e.closest("form[data-auto-submit='change']")?.requestSubmit()),t.replaceWith(e)}}handleButton(t){if(t.dataset.checkboxScope){let e=t.dataset.checkboxAction==="all";for(let i of this.querySelectorAll(`[data-checkbox-scope="${t.dataset.checkboxScope}"] input[type="checkbox"]`))i.disabled||(i.checked=e);return}}observeLayout(){let t=this.querySelector(".control-card"),e=this.querySelector(".details-card");if(!t||!e)return;let i=()=>{Math.abs(t.getBoundingClientRect().top-e.getBoundingClientRect().top)<2?e.style.setProperty("--details-card-height",`${t.getBoundingClientRect().height}px`):e.style.removeProperty("--details-card-height")};this.resizeObserver=new ResizeObserver(i),this.resizeObserver.observe(t),window.addEventListener("resize",i),requestAnimationFrame(i)}onSubmit=t=>{let e=t.target;if(!(e instanceof HTMLFormElement)||e.method.toLowerCase()!==Ps)return;t.preventDefault();let i=e.dataset.confirm;if(i&&e.dataset.confirmed!=="true"){this.confirmForm=e,this.confirmMessage=i,this.confirmOpen=!0;return}delete e.dataset.confirmed,this.dispatchMutation(e).catch(o=>this.markUnknown(o))};onCommand=t=>{t.stopPropagation();let{command:e,payload:i}=t.detail||{};this.dispatchCommand(e,new URL(e.replaceAll("_","-"),ro()).href,i||{}).catch(o=>this.markUnknown(o))};confirmMutation=()=>{let t=this.confirmForm;this.confirmOpen=!1,this.confirmForm=null,t&&(t.dataset.confirmed="true",t.requestSubmit())};async dispatchMutation(t){let e=Fs(t.action);return this.dispatchCommand(e,t.action,zs(t))}async dispatchCommand(t,e,i={}){let o={command_id:Rs(),command:t,generation:Number(this.state.operation_generation||0),payload:i},s=this.socket;if(Ds.has(t)&&s&&s.readyState===window.WebSocket.OPEN&&!this.replayPending){let l=String(this.nextRequestId++),d=new Promise((w,C)=>this.pending.set(l,{resolve:w,reject:C,sent:!1})),f=this.pending.get(l);s.send(JSON.stringify({id:l,...o})),f.sent=!0;let h=await d;if(!h.ok)throw new Error(h.message||"Command rejected");return h}if(s&&s.readyState!==window.WebSocket?.CLOSED)throw new Error("Connection state is unknown; the command was not retried.");let n=await fetch(e,{method:"POST",headers:{Accept:"application/json","Content-Type":"application/json","X-Requested-With":"fetch"},body:JSON.stringify(o)}),a=await n.json();if(!n.ok||!a.ok)throw new Error(a.message||"Command rejected");return await this.pollHttpCommand(o.command_id),a}async pollHttpCommand(t){let e=Date.now()+1e4;for(;Date.now()<e;){await this.loadHttpBaseline();let i=this.state.command_records?.[t]?.status;if(i==="terminal")return;if(i==="failed_unknown")throw new Error("Command outcome is unknown.");await new Promise(o=>setTimeout(o,100))}throw new Error("Command did not finish before the HTTP fallback timeout.")}connect(){if(this.connection="connecting",this.replayPending=!0,typeof window.WebSocket!="function"){this.socket=null,this.loadHttpBaseline();return}let t=new WebSocket(Bs());this.socket=t,t.addEventListener("open",()=>{this.connection="replaying",t.send(JSON.stringify({id:String(this.nextRequestId++),command:"replay"}))}),t.addEventListener("message",e=>this.receive(JSON.parse(e.data))),t.addEventListener("close",()=>{this.connection="disconnected";for(let e of this.pending.values())e.reject(new Error(e.sent?"Command outcome is unknown after disconnect.":"WebSocket unavailable."));this.pending.clear(),this.reconnectTimer=setTimeout(()=>this.connect(),1200)})}async loadHttpBaseline(){try{let e=await(await fetch("debug-snapshot")).json();this.applyBaseline(e),this.replayPending=!1,this.connection="http"}catch(t){this.connection="unknown",this.markUnknown(t)}}receive(t){if(t.type==="ready"||t.type==="replay"){this.applyBaseline(t),this.replayPending=!1,this.connection="connected";for(let e of this.queuedFrames.splice(0))this.receive(e);return}if(this.replayPending&&["state_patch","log_line","command_status"].includes(t.type)){this.queuedFrames.push(t);return}if(t.type==="state_patch"&&this.applyPatch(t),t.type==="state"&&this.applyBaseline(t),t.type==="result"&&t.id&&this.pending.has(t.id)){let e=this.pending.get(t.id);this.pending.delete(t.id),e.resolve(t)}}applyBaseline(t){t.state&&(this.state=structuredClone(t.state),this.revision=Number(t.revision??t.state_revision??t.state.state_revision??0),this.syncDom())}applyPatch(t){let e=Number(t.base_revision),i=Number(t.revision);if(!(i<=this.revision)){if(e!==this.revision){this.replayPending=!0,this.connection="replaying",this.socket?.send(JSON.stringify({id:String(this.nextRequestId++),command:"replay"}));return}this.state={...this.state,...t.patch||{}},this.revision=i,this.syncDom()}}syncDom(){let t=this.state.last_status==="running"||Object.values(this.state.command_records||{}).some(o=>["accepted","running","failed_unknown"].includes(o.status));for(let o of this.querySelectorAll("vaadin-button, vaadin-checkbox, vaadin-radio-group, vaadin-select"))o.matches("[data-read-only-control]")||(o.disabled=t||o.hasAttribute("data-server-disabled"));let e=this.querySelector("[data-status-code]");e&&(e.dataset.statusCode=this.state.last_status||"idle",e.textContent=this.state.last_status==="success"?"done":this.state.last_status||"idle");let i=this.querySelector("ha-ops-log");if(i){let o=Array.isArray(this.state.last_details)&&this.state.last_details.length?this.state.last_details:[this.state.last_message||""];i.lines=o,i.status=this.state.last_status||"idle"}this.upgradeControls()}isRunning(){return this.state.last_status==="running"||Object.values(this.state.command_records||{}).some(t=>["accepted","running","failed_unknown"].includes(t.status))}markUnknown(t){this.connection="unknown";let e=this.querySelector("#client-status");e&&(e.textContent=t.message)}};customElements.define("ha-ops-app",pi);
/*! Bundled license information:

@lit/reactive-element/css-tag.js:
  (**
   * @license
   * Copyright 2019 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

@lit/reactive-element/reactive-element.js:
lit-html/lit-html.js:
lit-element/lit-element.js:
  (**
   * @license
   * Copyright 2017 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

lit-html/is-server.js:
  (**
   * @license
   * Copyright 2022 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)

@vaadin/component-base/src/define.js:
@vaadin/component-base/src/dir-mixin.js:
@vaadin/component-base/src/element-mixin.js:
@vaadin/component-base/src/polylit-mixin.js:
@vaadin/component-base/src/dom-utils.js:
@vaadin/component-base/src/unique-id-utils.js:
@vaadin/component-base/src/slot-controller.js:
@vaadin/vaadin-themable-mixin/src/css-property-observer.js:
@vaadin/vaadin-themable-mixin/src/css-utils.js:
@vaadin/vaadin-themable-mixin/src/lumo-injector.js:
@vaadin/vaadin-themable-mixin/lumo-injection-mixin.js:
@vaadin/a11y-base/src/disabled-mixin.js:
@vaadin/a11y-base/src/keyboard-mixin.js:
@vaadin/a11y-base/src/active-mixin.js:
@vaadin/a11y-base/src/focus-utils.js:
@vaadin/a11y-base/src/focus-mixin.js:
@vaadin/a11y-base/src/tabindex-mixin.js:
@vaadin/field-base/src/styles/checkable-base-styles.js:
@vaadin/field-base/src/styles/field-base-styles.js:
@vaadin/a11y-base/src/delegate-focus-mixin.js:
@vaadin/component-base/src/slot-styles-mixin.js:
@vaadin/component-base/src/delegate-state-mixin.js:
@vaadin/field-base/src/input-mixin.js:
@vaadin/field-base/src/checked-mixin.js:
@vaadin/a11y-base/src/field-aria-controller.js:
@vaadin/field-base/src/error-controller.js:
@vaadin/field-base/src/helper-controller.js:
@vaadin/field-base/src/label-controller.js:
@vaadin/field-base/src/label-mixin.js:
@vaadin/field-base/src/validate-mixin.js:
@vaadin/field-base/src/field-mixin.js:
@vaadin/field-base/src/input-controller.js:
@vaadin/field-base/src/labelled-input-controller.js:
@vaadin/component-base/src/browser-utils.js:
@vaadin/a11y-base/src/focus-restoration-controller.js:
@vaadin/a11y-base/src/focus-trap-controller.js:
@vaadin/input-container/src/styles/vaadin-input-container-base-styles.js:
@vaadin/input-container/src/vaadin-input-container.js:
@vaadin/component-base/src/dir-utils.js:
@vaadin/a11y-base/src/styles/sr-only-styles.js:
@vaadin/field-base/src/styles/button-base-styles.js:
@vaadin/field-base/src/styles/input-field-shared-styles.js:
@vaadin/component-base/src/media-query-controller.js:
  (**
   * @license
   * Copyright (c) 2021 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/vaadin-usage-statistics/vaadin-usage-statistics-collect.js:
  (*! vaadin-dev-mode:start
    (function () {
  'use strict';
  
  var _typeof = typeof Symbol === "function" && typeof Symbol.iterator === "symbol" ? function (obj) {
    return typeof obj;
  } : function (obj) {
    return obj && typeof Symbol === "function" && obj.constructor === Symbol && obj !== Symbol.prototype ? "symbol" : typeof obj;
  };
  
  var classCallCheck = function (instance, Constructor) {
    if (!(instance instanceof Constructor)) {
      throw new TypeError("Cannot call a class as a function");
    }
  };
  
  var createClass = function () {
    function defineProperties(target, props) {
      for (var i = 0; i < props.length; i++) {
        var descriptor = props[i];
        descriptor.enumerable = descriptor.enumerable || false;
        descriptor.configurable = true;
        if ("value" in descriptor) descriptor.writable = true;
        Object.defineProperty(target, descriptor.key, descriptor);
      }
    }
  
    return function (Constructor, protoProps, staticProps) {
      if (protoProps) defineProperties(Constructor.prototype, protoProps);
      if (staticProps) defineProperties(Constructor, staticProps);
      return Constructor;
    };
  }();
  
  var getPolymerVersion = function getPolymerVersion() {
    return window.Polymer && window.Polymer.version;
  };
  
  var StatisticsGatherer = function () {
    function StatisticsGatherer(logger) {
      classCallCheck(this, StatisticsGatherer);
  
      this.now = new Date().getTime();
      this.logger = logger;
    }
  
    createClass(StatisticsGatherer, [{
      key: 'frameworkVersionDetectors',
      value: function frameworkVersionDetectors() {
        return {
          'Flow': function Flow() {
            if (window.Vaadin && window.Vaadin.Flow && window.Vaadin.Flow.clients) {
              var flowVersions = Object.keys(window.Vaadin.Flow.clients).map(function (key) {
                return window.Vaadin.Flow.clients[key];
              }).filter(function (client) {
                return client.getVersionInfo;
              }).map(function (client) {
                return client.getVersionInfo().flow;
              });
              if (flowVersions.length > 0) {
                return flowVersions[0];
              }
            }
          },
          'Vaadin Framework': function VaadinFramework() {
            if (window.vaadin && window.vaadin.clients) {
              var frameworkVersions = Object.values(window.vaadin.clients).filter(function (client) {
                return client.getVersionInfo;
              }).map(function (client) {
                return client.getVersionInfo().vaadinVersion;
              });
              if (frameworkVersions.length > 0) {
                return frameworkVersions[0];
              }
            }
          },
          'AngularJs': function AngularJs() {
            if (window.angular && window.angular.version && window.angular.version) {
              return window.angular.version.full;
            }
          },
          'Angular': function Angular() {
            if (window.ng) {
              var tags = document.querySelectorAll("[ng-version]");
              if (tags.length > 0) {
                return tags[0].getAttribute("ng-version");
              }
              return "Unknown";
            }
          },
          'Backbone.js': function BackboneJs() {
            if (window.Backbone) {
              return window.Backbone.VERSION;
            }
          },
          'React': function React() {
            var reactSelector = '[data-reactroot], [data-reactid]';
            if (!!document.querySelector(reactSelector)) {
              // React does not publish the version by default
              return "unknown";
            }
          },
          'Ember': function Ember() {
            if (window.Em && window.Em.VERSION) {
              return window.Em.VERSION;
            } else if (window.Ember && window.Ember.VERSION) {
              return window.Ember.VERSION;
            }
          },
          'jQuery': function (_jQuery) {
            function jQuery() {
              return _jQuery.apply(this, arguments);
            }
  
            jQuery.toString = function () {
              return _jQuery.toString();
            };
  
            return jQuery;
          }(function () {
            if (typeof jQuery === 'function' && jQuery.prototype.jquery !== undefined) {
              return jQuery.prototype.jquery;
            }
          }),
          'Polymer': function Polymer() {
            var version = getPolymerVersion();
            if (version) {
              return version;
            }
          },
          'LitElement': function LitElement() {
            var version = window.litElementVersions && window.litElementVersions[0];
            if (version) {
              return version;
            }
          },
          'LitHtml': function LitHtml() {
            var version = window.litHtmlVersions && window.litHtmlVersions[0];
            if (version) {
              return version;
            }
          },
          'Vue.js': function VueJs() {
            if (window.Vue) {
              return window.Vue.version;
            }
          }
        };
      }
    }, {
      key: 'getUsedVaadinElements',
      value: function getUsedVaadinElements(elements) {
        var version = getPolymerVersion();
        var elementClasses = void 0;
        // NOTE: In case you edit the code here, YOU MUST UPDATE any statistics reporting code in Flow.
        // Check all locations calling the method getEntries() in
        // https://github.com/vaadin/flow/blob/master/flow-server/src/main/java/com/vaadin/flow/internal/UsageStatistics.java#L106
        // Currently it is only used by BootstrapHandler.
        if (version && version.indexOf('2') === 0) {
          // Polymer 2: components classes are stored in window.Vaadin
          elementClasses = Object.keys(window.Vaadin).map(function (c) {
            return window.Vaadin[c];
          }).filter(function (c) {
            return c.is;
          });
        } else {
          // Polymer 3: components classes are stored in window.Vaadin.registrations
          elementClasses = window.Vaadin.registrations || [];
        }
        elementClasses.forEach(function (klass) {
          var version = klass.version ? klass.version : "0.0.0";
          elements[klass.is] = { version: version };
        });
      }
    }, {
      key: 'getUsedVaadinThemes',
      value: function getUsedVaadinThemes(themes) {
        ['Lumo', 'Material'].forEach(function (themeName) {
          var theme;
          var version = getPolymerVersion();
          if (version && version.indexOf('2') === 0) {
            // Polymer 2: themes are stored in window.Vaadin
            theme = window.Vaadin[themeName];
          } else {
            // Polymer 3: themes are stored in custom element registry
            theme = customElements.get('vaadin-' + themeName.toLowerCase() + '-styles');
          }
          if (theme && theme.version) {
            themes[themeName] = { version: theme.version };
          }
        });
      }
    }, {
      key: 'getFrameworks',
      value: function getFrameworks(frameworks) {
        var detectors = this.frameworkVersionDetectors();
        Object.keys(detectors).forEach(function (framework) {
          var detector = detectors[framework];
          try {
            var version = detector();
            if (version) {
              frameworks[framework] = { version: version };
            }
          } catch (e) {}
        });
      }
    }, {
      key: 'gather',
      value: function gather(storage) {
        var storedStats = storage.read();
        var gatheredStats = {};
        var types = ["elements", "frameworks", "themes"];
  
        types.forEach(function (type) {
          gatheredStats[type] = {};
          if (!storedStats[type]) {
            storedStats[type] = {};
          }
        });
  
        var previousStats = JSON.stringify(storedStats);
  
        this.getUsedVaadinElements(gatheredStats.elements);
        this.getFrameworks(gatheredStats.frameworks);
        this.getUsedVaadinThemes(gatheredStats.themes);
  
        var now = this.now;
        types.forEach(function (type) {
          var keys = Object.keys(gatheredStats[type]);
          keys.forEach(function (key) {
            if (!storedStats[type][key] || _typeof(storedStats[type][key]) != _typeof({})) {
              storedStats[type][key] = { firstUsed: now };
            }
            // Discards any previously logged version number
            storedStats[type][key].version = gatheredStats[type][key].version;
            storedStats[type][key].lastUsed = now;
          });
        });
  
        var newStats = JSON.stringify(storedStats);
        storage.write(newStats);
        if (newStats != previousStats && Object.keys(storedStats).length > 0) {
          this.logger.debug("New stats: " + newStats);
        }
      }
    }]);
    return StatisticsGatherer;
  }();
  
  var StatisticsStorage = function () {
    function StatisticsStorage(key) {
      classCallCheck(this, StatisticsStorage);
  
      this.key = key;
    }
  
    createClass(StatisticsStorage, [{
      key: 'read',
      value: function read() {
        var localStorageStatsString = localStorage.getItem(this.key);
        try {
          return JSON.parse(localStorageStatsString ? localStorageStatsString : '{}');
        } catch (e) {
          return {};
        }
      }
    }, {
      key: 'write',
      value: function write(data) {
        localStorage.setItem(this.key, data);
      }
    }, {
      key: 'clear',
      value: function clear() {
        localStorage.removeItem(this.key);
      }
    }, {
      key: 'isEmpty',
      value: function isEmpty() {
        var storedStats = this.read();
        var empty = true;
        Object.keys(storedStats).forEach(function (key) {
          if (Object.keys(storedStats[key]).length > 0) {
            empty = false;
          }
        });
  
        return empty;
      }
    }]);
    return StatisticsStorage;
  }();
  
  var StatisticsSender = function () {
    function StatisticsSender(url, logger) {
      classCallCheck(this, StatisticsSender);
  
      this.url = url;
      this.logger = logger;
    }
  
    createClass(StatisticsSender, [{
      key: 'send',
      value: function send(data, errorHandler) {
        var logger = this.logger;
  
        if (navigator.onLine === false) {
          logger.debug("Offline, can't send");
          errorHandler();
          return;
        }
        logger.debug("Sending data to " + this.url);
  
        var req = new XMLHttpRequest();
        req.withCredentials = true;
        req.addEventListener("load", function () {
          // Stats sent, nothing more to do
          logger.debug("Response: " + req.responseText);
        });
        req.addEventListener("error", function () {
          logger.debug("Send failed");
          errorHandler();
        });
        req.addEventListener("abort", function () {
          logger.debug("Send aborted");
          errorHandler();
        });
        req.open("POST", this.url);
        req.setRequestHeader("Content-Type", "application/json");
        req.send(data);
      }
    }]);
    return StatisticsSender;
  }();
  
  var StatisticsLogger = function () {
    function StatisticsLogger(id) {
      classCallCheck(this, StatisticsLogger);
  
      this.id = id;
    }
  
    createClass(StatisticsLogger, [{
      key: '_isDebug',
      value: function _isDebug() {
        return localStorage.getItem("vaadin." + this.id + ".debug");
      }
    }, {
      key: 'debug',
      value: function debug(msg) {
        if (this._isDebug()) {
          console.info(this.id + ": " + msg);
        }
      }
    }]);
    return StatisticsLogger;
  }();
  
  var UsageStatistics = function () {
    function UsageStatistics() {
      classCallCheck(this, UsageStatistics);
  
      this.now = new Date();
      this.timeNow = this.now.getTime();
      this.gatherDelay = 10; // Delay between loading this file and gathering stats
      this.initialDelay = 24 * 60 * 60;
  
      this.logger = new StatisticsLogger("statistics");
      this.storage = new StatisticsStorage("vaadin.statistics.basket");
      this.gatherer = new StatisticsGatherer(this.logger);
      this.sender = new StatisticsSender("https://tools.vaadin.com/usage-stats/submit", this.logger);
    }
  
    createClass(UsageStatistics, [{
      key: 'maybeGatherAndSend',
      value: function maybeGatherAndSend() {
        var _this = this;
  
        if (localStorage.getItem(UsageStatistics.optOutKey)) {
          return;
        }
        this.gatherer.gather(this.storage);
        setTimeout(function () {
          _this.maybeSend();
        }, this.gatherDelay * 1000);
      }
    }, {
      key: 'lottery',
      value: function lottery() {
        return true;
      }
    }, {
      key: 'currentMonth',
      value: function currentMonth() {
        return this.now.getYear() * 12 + this.now.getMonth();
      }
    }, {
      key: 'maybeSend',
      value: function maybeSend() {
        var firstUse = Number(localStorage.getItem(UsageStatistics.firstUseKey));
        var monthProcessed = Number(localStorage.getItem(UsageStatistics.monthProcessedKey));
  
        if (!firstUse) {
          // Use a grace period to avoid interfering with tests, incognito mode etc
          firstUse = this.timeNow;
          localStorage.setItem(UsageStatistics.firstUseKey, firstUse);
        }
  
        if (this.timeNow < firstUse + this.initialDelay * 1000) {
          this.logger.debug("No statistics will be sent until the initial delay of " + this.initialDelay + "s has passed");
          return;
        }
        if (this.currentMonth() <= monthProcessed) {
          this.logger.debug("This month has already been processed");
          return;
        }
        localStorage.setItem(UsageStatistics.monthProcessedKey, this.currentMonth());
        // Use random sampling
        if (this.lottery()) {
          this.logger.debug("Congratulations, we have a winner!");
        } else {
          this.logger.debug("Sorry, no stats from you this time");
          return;
        }
  
        this.send();
      }
    }, {
      key: 'send',
      value: function send() {
        // Ensure we have the latest data
        this.gatherer.gather(this.storage);
  
        // Read, send and clean up
        var data = this.storage.read();
        data["firstUse"] = Number(localStorage.getItem(UsageStatistics.firstUseKey));
        data["usageStatisticsVersion"] = UsageStatistics.version;
        var info = 'This request contains usage statistics gathered from the application running in development mode. \n\nStatistics gathering is automatically disabled and excluded from production builds.\n\nFor details and to opt-out, see https://github.com/vaadin/vaadin-usage-statistics.\n\n\n\n';
        var self = this;
        this.sender.send(info + JSON.stringify(data), function () {
          // Revert the 'month processed' flag
          localStorage.setItem(UsageStatistics.monthProcessedKey, self.currentMonth() - 1);
        });
      }
    }], [{
      key: 'version',
      get: function get$1() {
        return '2.1.2';
      }
    }, {
      key: 'firstUseKey',
      get: function get$1() {
        return 'vaadin.statistics.firstuse';
      }
    }, {
      key: 'monthProcessedKey',
      get: function get$1() {
        return 'vaadin.statistics.monthProcessed';
      }
    }, {
      key: 'optOutKey',
      get: function get$1() {
        return 'vaadin.statistics.optout';
      }
    }]);
    return UsageStatistics;
  }();
  
  try {
    window.Vaadin = window.Vaadin || {};
    window.Vaadin.usageStatsChecker = window.Vaadin.usageStatsChecker || new UsageStatistics();
    window.Vaadin.usageStatsChecker.maybeGatherAndSend();
  } catch (e) {
    // Intentionally ignored as this is not a problem in the app being developed
  }
  
  }());
  
    vaadin-dev-mode:end **)

@vaadin/component-base/src/async.js:
  (**
   * @license
   * Copyright (c) 2017 The Polymer Project Authors. All rights reserved.
   * This code may only be used under the BSD style license found at http://polymer.github.io/LICENSE.txt
   * The complete set of authors may be found at http://polymer.github.io/AUTHORS.txt
   * The complete set of contributors may be found at http://polymer.github.io/CONTRIBUTORS.txt
   * Code distributed by Google as part of the polymer project is also
   * subject to an additional IP rights grant found at http://polymer.github.io/PATENTS.txt
   *)

@vaadin/component-base/src/debounce.js:
@vaadin/component-base/src/gestures.js:
  (**
  @license
  Copyright (c) 2017 The Polymer Project Authors. All rights reserved.
  This code may only be used under the BSD style license found at http://polymer.github.io/LICENSE.txt
  The complete set of authors may be found at http://polymer.github.io/AUTHORS.txt
  The complete set of contributors may be found at http://polymer.github.io/CONTRIBUTORS.txt
  Code distributed by Google as part of the polymer project is also
  subject to an additional IP rights grant found at http://polymer.github.io/PATENTS.txt
  *)

@vaadin/component-base/src/path-utils.js:
@vaadin/component-base/src/slot-observer.js:
@vaadin/a11y-base/src/aria-id-reference.js:
@vaadin/select/src/button-controller.js:
  (**
   * @license
   * Copyright (c) 2023 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/component-base/src/tooltip-controller.js:
@vaadin/a11y-base/src/announce.js:
@vaadin/component-base/src/slot-child-observe-controller.js:
@vaadin/a11y-base/src/keyboard-direction-mixin.js:
  (**
   * @license
   * Copyright (c) 2022 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/component-base/src/css-utils.js:
  (**
   * @license
   * Copyright (c) 2025 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/component-base/src/warnings.js:
@vaadin/vaadin-themable-mixin/src/lumo-modules.js:
  (**
   * @license
   * Copyright (c) 2000 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/vaadin-themable-mixin/vaadin-theme-property-mixin.js:
@vaadin/vaadin-themable-mixin/vaadin-themable-mixin.js:
@vaadin/component-base/src/styles/style-props.js:
@vaadin/button/src/styles/vaadin-button-base-styles.js:
@vaadin/button/src/vaadin-button-mixin.js:
@vaadin/button/src/vaadin-button.js:
@vaadin/checkbox/src/styles/vaadin-checkbox-base-styles.js:
@vaadin/checkbox/src/vaadin-checkbox-mixin.js:
@vaadin/checkbox/src/vaadin-checkbox.js:
@vaadin/overlay/src/vaadin-overlay-focus-mixin.js:
@vaadin/overlay/src/vaadin-overlay-stack-mixin.js:
@vaadin/overlay/src/vaadin-overlay-mixin.js:
@vaadin/overlay/src/styles/vaadin-overlay-base-styles.js:
@vaadin/dialog/src/styles/vaadin-dialog-overlay-base-styles.js:
@vaadin/dialog/src/vaadin-dialog-size-mixin.js:
@vaadin/progress-bar/src/styles/vaadin-progress-bar-base-styles.js:
@vaadin/progress-bar/src/vaadin-progress-mixin.js:
@vaadin/progress-bar/src/vaadin-progress-bar.js:
@vaadin/radio-group/src/styles/vaadin-radio-button-base-styles.js:
@vaadin/radio-group/src/vaadin-radio-button-mixin.js:
@vaadin/radio-group/src/vaadin-radio-button.js:
@vaadin/radio-group/src/styles/vaadin-radio-group-base-styles.js:
@vaadin/radio-group/src/vaadin-radio-group-mixin.js:
@vaadin/radio-group/src/vaadin-radio-group.js:
@vaadin/item/src/styles/vaadin-item-base-styles.js:
@vaadin/item/src/vaadin-item-mixin.js:
@vaadin/select/src/vaadin-select-item.js:
@vaadin/a11y-base/src/list-mixin.js:
@vaadin/list-box/src/styles/vaadin-list-box-base-styles.js:
@vaadin/select/src/vaadin-select-list-box.js:
@vaadin/select/src/styles/vaadin-select-overlay-base-styles.js:
@vaadin/overlay/src/vaadin-overlay-position-mixin.js:
@vaadin/select/src/vaadin-select-overlay-mixin.js:
@vaadin/select/src/vaadin-select-overlay.js:
@vaadin/select/src/styles/vaadin-select-value-button-base-styles.js:
@vaadin/select/src/vaadin-select-value-button.js:
@vaadin/select/src/styles/vaadin-select-base-styles.js:
@vaadin/select/src/vaadin-select-base-mixin.js:
@vaadin/select/src/vaadin-select.js:
  (**
   * @license
   * Copyright (c) 2017 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/overlay/src/vaadin-overlay-utils.js:
  (**
   * @license
   * Copyright (c) 2024 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

@vaadin/confirm-dialog/src/styles/vaadin-confirm-dialog-overlay-base-styles.js:
@vaadin/confirm-dialog/src/vaadin-confirm-dialog-overlay.js:
@vaadin/confirm-dialog/src/vaadin-confirm-dialog-mixin.js:
@vaadin/confirm-dialog/src/vaadin-confirm-dialog.js:
@vaadin/field-base/src/styles/group-base-styles.js:
  (**
   * @license
   * Copyright (c) 2018 - 2026 Vaadin Ltd.
   * This program is available under Apache License Version 2.0, available at https://vaadin.com/license/
   *)

lit-html/directives/if-defined.js:
  (**
   * @license
   * Copyright 2018 Google LLC
   * SPDX-License-Identifier: BSD-3-Clause
   *)
*/
