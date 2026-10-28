"""GĐ9.2 Prompt Canvas UI (inside the Prompt Hub page, ui_ls2.PROMPTS): the prompt on the left, a chat with an AI rewriter on the right.
English source strings (Vietnamese through i18n_dict.json). API: app/prompt_lab.py (/api/hub/canvas/*). Own implementation (the Chat Canvas is a React component
bound to the chat runtime; the Hub is server-rendered), same look: dark/light tokens, chips, panes."""

CANVAS_CSS = """<style>
.cvx{display:grid;grid-template-columns:minmax(0,1fr) 400px;min-height:calc(100vh - 44px)}.cvx>aside{border-left:1px solid var(--bd2);display:flex;flex-direction:column;min-height:0;max-height:calc(100vh - 44px);position:sticky;top:0}
.cvx>section{min-width:0}.cvt{width:100%;min-height:340px;font-family:var(--mono);font-size:13px;line-height:1.55;resize:vertical;white-space:pre-wrap}
.cvd,.cvs{border:1px solid var(--bd2);border-radius:var(--r-lg);padding:12px 14px;font-family:var(--mono);font-size:13px;line-height:1.7;white-space:pre-wrap;word-break:break-word;background:var(--s1)}
.cvd del{background:var(--er-bg);color:var(--er);text-decoration:line-through;border-radius:3px;padding:0 1px}.cvd ins{background:var(--ok-bg);color:var(--ok);text-decoration:none;border-radius:3px;padding:0 1px}
.cvd .hk{border-radius:4px;outline:1px dashed transparent}.cvd .hk.pending{outline-color:var(--bd2)}.cvd .hk.acc{outline:1px solid var(--ok)}.cvd .hk.rej{outline:1px solid var(--bd2);opacity:.65}.cvd .hk.rej ins{text-decoration:line-through;background:transparent;color:var(--t3)}.cvd .hk.rej del{text-decoration:none;background:transparent;color:var(--t1)}
.cvd .hkb{display:inline-flex;gap:2px;margin-left:3px;vertical-align:middle;font-family:var(--font)}.cvd .hkb button{height:20px;width:20px;padding:0;display:inline-flex;align-items:center;justify-content:center}.cvd .ws{opacity:.7}
.cvbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 0}.cvbar .sp{flex:1}
.cvchat{flex:1;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px;min-height:120px}
.cvm{max-width:92%;padding:8px 11px;border-radius:12px;font-size:13px;line-height:1.45;white-space:pre-wrap}.cvm.u{align-self:flex-end;background:var(--sel);color:var(--t1)}.cvm.a{align-self:flex-start;background:var(--s2);border:1px solid var(--bd3)}.cvm.e{align-self:flex-start;background:var(--er-bg);color:var(--er)}
.cvm small{display:block;color:var(--t3);margin-top:4px}.cvqa{display:flex;gap:6px;flex-wrap:wrap;padding:8px 14px;border-top:1px solid var(--bd3)}.cvqa button{height:26px;font-size:12px;border-radius:99px}
.cvin{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--bd2)}.cvin textarea{flex:1;min-height:38px;max-height:120px;resize:none;font-size:13px;font-family:var(--font)}
.cvsel{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2)}
@media(max-width:1000px){.cvx{grid-template-columns:1fr}.cvx>aside{position:static;max-height:none;border-left:0;border-top:1px solid var(--bd2)}}
</style>"""

CANVAS_JS = r"""
/* ---------------- Prompt Canvas (GĐ9.2) ---------------- */
const CV_VARS=s=>{const o=new Set();for(const m of String(s).matchAll(/\{\{\s*([A-Za-z_][\w.]*)\s*\}\}|(?<!\{)\{([A-Za-z_]\w*)\}(?!\})/g))o.add(m[1]?'{{'+m[1]+'}}':'{'+m[2]+'}');return o};
const CV_CP=(s,i)=>[...s.slice(0,i)].length;
const CV_ACTIONS=[['shorter','Shorter'],['examples','Add examples'],['tone','Change tone'],['clearer','Clearer'],['translate','Translate VI/EN'],['safety','Add safety rules'],['feedback','Fix from feedback']];
const CV_TONES=[['formal','Formal'],['friendly','Friendly'],['professional','Professional']];
function openCanvas(d,v,id,close){
 const base=v.template,CV={text:base,sel:null,prop:null,busy:false,hist:[],ac:null};const pid=d.prompt.id;
 const missingNow=()=>[...CV_VARS(base)].filter(x=>!CV_VARS(CV.text).has(x));
 $('pr').innerHTML=`<div class="cvx"><section><div class="ph">${hdr('sparkles','Canvas',`<span class="chip mono">${esc(d.prompt.name)} · v${v.version}</span>`,`<button class="g" id="cvx_close">${ICN('arrow-left')}Back to prompt</button><button id="cvx_save">${ICN('save')}Save commit</button>`)}</div>
  <div class="bodyx"><div class="cvbar" id="cvx_bar"></div><div id="cvx_main"></div><div id="cvx_warn"></div></div></section>
  <aside><div style="padding:12px 14px;border-bottom:1px solid var(--bd2)"><b>Rewrite with AI</b><div class="muted" style="font-size:12px;margin-top:2px">Pick a quick action or describe the change. Select part of the prompt to rewrite only that part.</div></div>
   <div class="cvchat" id="cvx_chat"></div><div class="cvqa" id="cvx_qa"></div>
   <div class="cvin"><textarea id="cvx_in" placeholder="Describe how to change the prompt…" rows="1"></textarea><button id="cvx_send" title="Send">${ICN('send')}</button></div></aside></div>`;
 const say=(cls,html)=>{const m=el('div','cvm '+cls);m.innerHTML=html;$('cvx_chat').appendChild(m);$('cvx_chat').scrollTop=1e9;return m};
 say('a','Hi! Tell me what to change in this prompt, or use a quick action below.');
 /* left: editor or review */
 function bar(){const b=$('cvx_bar');
  if(CV.busy){b.innerHTML=`<span class="muted">${ICN('loader')} Writing…</span><span class="sp"></span><button class="g" id="cvx_stop">${ICN('x')}Stop</button>`;$('cvx_stop').onclick=()=>CV.ac&&CV.ac.abort();return}
  if(CV.prop){const st=CV.prop.status,n=Object.values(st).filter(x=>x==='acc').length,tot=CV.prop.segments.filter(s=>s.t==='chg').length;
   b.innerHTML=`<span class="chip pu">${tot}<span> changes</span></span><span class="chip ok">+${CV.prop.stats.added_words}</span><span class="chip er">−${CV.prop.stats.removed_words}</span><span class="muted" style="font-size:12px">${n}<span> accepted</span></span><span class="sp"></span>
    <button class="g" id="cvx_rej">${ICN('x')}Reject all</button><button class="g" id="cvx_acc">${ICN('check')}Accept all</button><button id="cvx_apply" ${n?'':'disabled'}>${ICN('check')}Apply accepted</button>`;
   $('cvx_rej').onclick=()=>{for(const k in st)st[k]='rej';review()};$('cvx_acc').onclick=()=>{for(const k in st)st[k]='acc';review()};$('cvx_apply').onclick=apply;return}
  b.innerHTML=`<span class="cvsel" id="cvx_sel"></span><span class="sp"></span><span class="muted" style="font-size:12px"><span>Variables</span> ${[...CV_VARS(CV.text)].map(x=>`<span class="var">${esc(x)}</span>`).join(' ')||'–'}</span>`;selInfo()}
 function selInfo(){const s=$('cvx_sel');if(!s)return;s.innerHTML=CV.sel?`${ICN('sparkles')}<span>Rewriting only the selected text</span><b>${[...CV.sel.text].length}</b><span>characters</span><button class="gh s" id="cvx_clr">${ICN('x')}</button>`:`<span class="muted">Tip: select a part of the prompt to rewrite only that part.</span>`;
  if(CV.sel)$('cvx_clr').onclick=()=>{CV.sel=null;const t=$('cvx_t');if(t)t.setSelectionRange(t.selectionEnd,t.selectionEnd);selInfo()}}
 function edit(){const m=$('cvx_main');m.innerHTML=`<textarea id="cvx_t" class="cvt" spellcheck="false"></textarea>`;const t=$('cvx_t');t.value=CV.text;
  t.oninput=()=>{CV.text=t.value;CV.sel=null;selInfo();warn()};
  const grab=()=>{if(t.selectionStart!==t.selectionEnd){const a=t.selectionStart,z=t.selectionEnd;CV.sel={start:CV_CP(t.value,a),end:CV_CP(t.value,z),text:t.value.slice(a,z)}}else CV.sel=null;selInfo()};
  t.onselect=grab;t.onmouseup=grab;t.onkeyup=grab;bar();warn()}
 function warn(){const w=$('cvx_warn'),m=missingNow();w.innerHTML=m.length?`<div class="card" style="border-color:var(--er)"><b style="color:var(--er)">${ICN('circle-alert')}<span>These variables are missing from the prompt:</span></b> ${m.map(x=>`<span class="var">${esc(x)}</span>`).join(' ')}</div>`:''}
 const ws=x=>x.trim()===''?`<span class="ws">${esc(x).replace(/\n/g,'¶\n').replace(/ /g,'·')||'∅'}</span>`:esc(x);
 function review(){const P=CV.prop,m=$('cvx_main');
  m.innerHTML=`<div class="cvd" id="cvx_d">${P.segments.map(s=>s.t==='eq'?esc(s.text):`<span class="hk ${P.status[s.id]}" data-h="${s.id}">${s.old?`<del>${ws(s.old)}</del>`:''}${s.new?`<ins>${ws(s.new)}</ins>`:''}<span class="hkb"><button class="g s" data-a="${s.id}" title="Accept">${ICN('check')}</button><button class="g s" data-r="${s.id}" title="Reject">${ICN('x')}</button></span></span>`).join('')}</div>
   ${P.missing.length?`<div class="card" style="border-color:var(--er);margin-top:10px"><b style="color:var(--er)">${ICN('circle-alert')}<span>The rewrite dropped these variables:</span></b> ${P.missing.map(x=>`<span class="var">${esc(x)}</span>`).join(' ')}</div>`:''}`;
  m.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{P.status[b.dataset.a]=P.status[b.dataset.a]==='acc'?'pending':'acc';review()});m.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{P.status[b.dataset.r]=P.status[b.dataset.r]==='rej'?'pending':'rej';review()});bar();warn()}
 function apply(){const P=CV.prop;const t=P.segments.map(s=>s.t==='eq'?s.text:P.status[s.id]==='acc'?s.new:s.old).join('');const n=Object.values(P.status).filter(x=>x==='acc').length;CV.text=t;CV.prop=null;CV.sel=null;
  say('a',`<span>Applied</span> <b>${n}</b> <span>changes.</span>`);edit()}
 /* right: chat + quick actions */
 $('cvx_qa').innerHTML=CV_ACTIONS.map(([k,l])=>`<button class="g" data-qa="${k}">${l}</button>`).join('');
 $('cvx_qa').querySelectorAll('[data-qa]').forEach(b=>b.onclick=()=>quick(b.dataset.qa));
 function quick(k){if(CV.busy)return;
  if(k==='tone')return pane('Change tone',`<div class="muted" style="margin-bottom:8px">Pick a tone. The AI keeps the content and variables.</div><div class="row" style="flex-wrap:wrap;gap:8px">${CV_TONES.map(([t,l])=>`<button class="g" data-tone="${t}">${l}</button>`).join('')}</div><label>Or describe the tone</label><input id="cvx_tone" style="width:100%" placeholder="e.g. warm and playful"><div class="row" style="margin-top:12px"><span class="sp"></span><button id="cvx_tgo">Rewrite</button></div>`,p=>{
   const go=(txt)=>{closePane();send('tone',txt,'Change tone: '+txt)};p.querySelectorAll('[data-tone]').forEach(b=>b.onclick=()=>go(b.dataset.tone));p.querySelector('#cvx_tgo').onclick=()=>{const x=p.querySelector('#cvx_tone').value.trim();if(x)go(x)}});
  if(k==='feedback')return fbPane();
  const l=(CV_ACTIONS.find(a=>a[0]===k)||[])[1];send(k,null,l)}
 async function fbPane(){let rows=[];try{rows=await api('api/hub/canvas/feedback')}catch(e){}
  pane('Fix from feedback',`<div class="muted" style="margin-bottom:8px">Pick a trace that received feedback, or write the feedback yourself.</div>
   ${rows.length?`<div class="card" style="padding:0;max-height:260px;overflow:auto">${rows.map(r=>`<div class="cm" data-run="${r.run_id}"><div style="flex:1;min-width:0"><div class="trunc" style="color:var(--t1)">${esc(r.comment)}</div><small class="muted">${esc(r.name||'')} · ${esc(r.key)}${r.score!=null?' · '+r.score:''}</small></div></div>`).join('')}</div>`:`<div class="muted">No trace with a feedback comment yet.</div>`}
   <label>Feedback</label><textarea id="cvx_fb" rows="3" style="width:100%" placeholder="What was wrong with the answers?"></textarea><div class="row" style="margin-top:12px"><span class="sp"></span><button id="cvx_fgo">Rewrite</button></div>`,p=>{
   p.querySelectorAll('[data-run]').forEach(c=>c.onclick=()=>{closePane();send('feedback',null,'Fix from feedback',{run_id:c.dataset.run})});
   p.querySelector('#cvx_fgo').onclick=()=>{const x=p.querySelector('#cvx_fb').value.trim();if(!x)return flash('Write the feedback first',1);closePane();send('feedback',null,'Fix from feedback',{feedback:x})}})}
 async function send(action,instruction,label,extra={}){if(CV.busy)return;if(CV.prop){if(!confirm('Discard the proposed changes?'))return;CV.prop=null}
  const sel=CV.sel;const body={template:CV.text,action,instruction:instruction||null,history:CV.hist.slice(-6),stream:true,...extra};if(sel)body.selection={start:sel.start,end:sel.end,text:sel.text};
  CV.hist.push({role:'user',content:label});say('u',esc(label)+(sel?`<small><span>Selected text only</span></small>`:''));CV.busy=true;edit();$('cvx_main').innerHTML=`<div class="cvs" id="cvx_stream"></div>`;bar();
  const box=$('cvx_stream');let acc='';const ac=CV.ac=new AbortController();
  try{const r=await fetch('api/hub/canvas/rewrite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:ac.signal});
   if(!r.ok){let j={};try{j=await r.json()}catch(e){}const dd=j.detail;throw new Error(typeof dd==='string'?dd:(dd&&(dd.message||dd.error))||('HTTP '+r.status))}
   const rd=r.body.getReader(),dec=new TextDecoder();let buf='',done=null;
   while(true){const {value,done:fin}=await rd.read();if(fin)break;buf+=dec.decode(value,{stream:true});let i;
    while((i=buf.indexOf('\n\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+2);if(!line.startsWith('data: '))continue;const e=JSON.parse(line.slice(6));
     if(e.type==='delta'){acc+=e.text;box.textContent=acc}else if(e.type==='retry'){acc='';box.textContent='';say('a',`<span>Some variables were dropped, trying again…</span> ${e.missing.map(x=>`<span class="var">${esc(x)}</span>`).join(' ')}`)}
     else if(e.type==='error')throw new Error(e.message);else if(e.type==='done')done=e}}
   CV.busy=false;if(!done)throw new Error('The connection closed before the rewrite finished');
   const segs=done.segments,ch=segs.filter(s=>s.t==='chg');
   if(!ch.length){say('a','The text did not change.');edit();return}
   CV.prop={segments:segs,stats:done.stats,missing:done.missing_variables||[],status:Object.fromEntries(ch.map(s=>[s.id,'pending']))};
   CV.hist.push({role:'assistant',content:`Proposed ${done.stats.hunks} changes`});
   say('a',`<b>${done.stats.hunks}</b> <span>changes proposed.</span> <span>Accept or reject each change in the diff.</span><small>${esc(done.model)}${done.retried?' · ✓':''}</small>`);review()
  }catch(e){CV.busy=false;if(e.name==='AbortError')say('e','Stopped.');else say('e',esc(e.message));edit()}}
 const inp=$('cvx_in');const go=()=>{const x=inp.value.trim();if(!x||CV.busy)return;inp.value='';send('custom',x,x)};
 $('cvx_send').onclick=go;inp.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go()}};
 $('cvx_close').onclick=()=>{if(CV.text!==base&&!confirm('Leave the Canvas? Unsaved edits will be lost.'))return;close(v.version)};
 $('cvx_save').onclick=()=>{if(CV.prop)return flash('Apply or reject the proposed changes first',1);if(CV.text===base)return flash('No changes to commit',1);
  const stats=window._cvstats||null;
  pane('Save commit',`<div class="muted" style="margin-bottom:8px"><span>New commit on top of</span> <b>v${v.version}</b></div><label>Commit message</label><input id="cvx_msg" style="width:100%" placeholder="What changed and why">
   <label>Tag (optional)</label><input id="cvx_tag" list="cvx_tags" style="width:100%" placeholder="e.g. staging"><datalist id="cvx_tags"><option value="production"><option value="staging"></datalist>
   ${missingNow().length?`<div class="card" style="border-color:var(--er);margin-top:10px"><b style="color:var(--er)">${ICN('circle-alert')}<span>These variables are missing from the prompt:</span></b> ${missingNow().map(x=>`<span class="var">${esc(x)}</span>`).join(' ')}</div>`:''}
   <div class="row" style="margin-top:14px"><span class="sp"></span><button class="g" onclick="closePane()">Cancel</button><button id="cvx_do">${ICN('save')}Save</button></div>`,p=>{
   p.querySelector('#cvx_do').onclick=async()=>{const b={prompt_id:pid,base_version:v.version,template:CV.text,message:p.querySelector('#cvx_msg').value||null,tag:p.querySelector('#cvx_tag').value||null};
    try{let r;try{r=await api('api/hub/canvas/commit','POST',b)}catch(e){if(/variables would be lost/i.test(e.message)&&confirm('Save anyway? Some template variables would be lost.')){r=await api('api/hub/canvas/commit','POST',{...b,allow_removed_variables:true})}else throw e}
     closePane();flash('Committed v'+r.version);CV.text=base;close(r.version)}catch(e){flash(e.message,1)}}})};
 edit()}
"""
