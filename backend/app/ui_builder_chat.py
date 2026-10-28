"""GĐ8.7 (23/10/2026): /builder-chat "Create agent by chat": chat bubbles (the builder's questions, at most 5), then an editable summary card
(name, description, instructions, tools, model, channel, monthly cost cap, file skills) with Create / Test run (opens the new agent's Test tab).
English source strings; Vietnamese through i18n_dict.json. APIs: /api/builder/chat, /api/builder/chat/create (app/builder_chat.py)."""
from ui_ls3 import P

CSS = """<style>
.bc{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;align-items:start}.bc .log{display:grid;gap:8px;align-content:start;min-height:46vh;max-height:62vh;overflow:auto;padding:14px}
.bc .inp{display:flex;gap:8px;padding:10px;border-top:1px solid var(--bd2)}.bc .inp textarea{flex:1;font-family:var(--font);resize:vertical}
.bc .sum{display:grid;gap:6px;padding:14px}.bc .sum label{margin:0;font-size:12px;color:var(--t2)}.bc .sum .acts{display:flex;gap:8px;margin-top:6px}
.bc .warn{font-size:12.5px;color:var(--warn,#b45309)}@media(max-width:900px){.bc{grid-template-columns:1fr}}
</style>"""

BUILDER_CHAT = P("builder", "Create agent by chat", CSS + """<div class="ph" id="h"></div><div class="bodyx"><div class="bc">
<div class="card" style="padding:0;display:flex;flex-direction:column"><div class="log" id="log"></div>
<div class="inp"><textarea id="msg" rows="2" placeholder="Describe the agent you need, e.g. an assistant that answers customers about prices on Zalo"></textarea><button id="send">Send</button></div></div>
<div class="card" id="sum" style="display:none;padding:0"></div></div></div>""", r"""
let MSGS=[],DRAFT=null,CREATED=null,MODELS=[],BUSY=false;
const SK=['docx','xlsx','pptx','pdf'],CH=['none','telegram','zalo','slack'];
function bubble(role,text){const d=document.createElement('div');d.className='tmsg '+(role==='user'?'u':'a');d.textContent=text;$('log').appendChild(d);$('log').scrollTop=1e9}
function redraw(){$('log').innerHTML='';bubble('assistant','Hi! Tell me what the agent should do. I will ask at most 5 short questions, then fill in the configuration for you.');MSGS.forEach(m=>bubble(m.role,m.content))}
async function send(){const t=$('msg').value.trim();if(!t||BUSY)return;MSGS.push({role:'user',content:t});$('msg').value='';redraw();BUSY=true;$('send').disabled=true;
 const w=document.createElement('div');w.className='tmsg a muted';w.textContent='Thinking…';$('log').appendChild(w);
 try{const r=await api('api/builder/chat','POST',{messages:MSGS});w.remove();
  if(r.type==='question'){MSGS.push({role:'assistant',content:r.question});redraw()}
  else{DRAFT=r;CREATED=null;bubble('assistant','Here is the draft. Check the summary, edit anything, then click Create.');summary()}}
 catch(e){w.remove();MSGS.pop();$('msg').value=t;redraw();flash(e.message,1)}
 BUSY=false;$('send').disabled=false}
function summary(){const d=DRAFT.draft,s=$('sum');s.style.display='';
 const ms=[...new Set([d.model,...MODELS])].filter(Boolean);
 s.innerHTML=`<div class="sum"><b>Agent summary</b>
 ${(DRAFT.warnings||[]).map(x=>`<div class="warn">${esc(x)}</div>`).join('')}
 <label>Name</label><input id="dn" value="${esc(d.name)}">
 <label>Description</label><textarea id="dd" rows="2" style="font-family:var(--font)">${esc(d.description)}</textarea>
 <label>Instructions</label><textarea id="di" rows="8" style="font-family:var(--font)">${esc(d.instructions)}</textarea>
 <label>Tools (comma separated)</label><input id="dt" class="mono" value="${esc((d.tools||[]).join(', '))}">
 <label>Model</label><select id="dm">${ms.map(x=>`<option ${x===d.model?'selected':''}>${esc(x)}</option>`).join('')}</select>
 <label>Channel</label><select id="dc">${CH.map(x=>`<option value="${x}" ${x===d.channel?'selected':''}>${x==='none'?'No channel':x}</option>`).join('')}</select>
 <label>Cost cap (USD / month)</label><input id="dcap" type="number" min="0.1" max="100" step="0.5" value="${d.cost_cap_usd_month}">
 <label>File skills</label><div class="row">${SK.map(x=>`<label style="margin:0;font-size:13px"><input type="checkbox" class="dsk" value="${x}" ${(d.skills||[]).includes(x)?'checked':''}> ${x}</label>`).join(' ')}</div>
 ${DRAFT.channel_hint?`<small class="muted">${esc(DRAFT.channel_hint)}</small>`:''}
 <div id="cres"></div>
 <div class="acts"><button id="bcreate">Create</button><button id="btest" class="s">Test run</button></div>
 <small class="muted" id="nyet">Nothing is created until you click Create.</small></div>`;
 $('bcreate').onclick=()=>create(false);$('btest').onclick=()=>create(true)}
function edited(){return {name:$('dn').value,description:$('dd').value,instructions:$('di').value,tools:$('dt').value.split(',').map(x=>x.trim()).filter(Boolean),
 model:$('dm').value,channel:$('dc').value,cost_cap_usd_month:+$('dcap').value||2,skills:[...document.querySelectorAll('.dsk:checked')].map(x=>x.value)}}
async function create(test){if(CREATED){if(test)location.href=CREATED.test_url;return}
 $('bcreate').disabled=$('btest').disabled=true;
 try{CREATED=await api('api/builder/chat/create','POST',{draft:edited()});
  if(test){location.href=CREATED.test_url;return}
  $('cres').innerHTML=`<div class="ok">Agent created: <a href="builder?id=${CREATED.id}">#${CREATED.id} ${esc(CREATED.draft.name)}</a></div>${(CREATED.warnings||[]).map(x=>`<div class="warn">${esc(x)}</div>`).join('')}`;$('nyet').style.display='none';
  $('bcreate').textContent='Agent saved';$('btest').disabled=false}
 catch(e){$('bcreate').disabled=$('btest').disabled=false;flash(e.message,1)}}
(async()=>{$('h').innerHTML=hdr('sparkles','Create agent by chat','<span class="muted">Answer a few questions; AI fills in the configuration.</span>',`<a href="builder?new=1">Classic form</a>`);
 redraw();$('send').onclick=send;$('msg').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}};
 MODELS=await api('api/models').then(m=>(Array.isArray(m)?m:(m.models||[])).map(x=>x.id||x.name||x)).catch(()=>[])})();
""")

PAGES = {"/builder-chat": BUILDER_CHAT}
