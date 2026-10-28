"""GĐ12.89 Skills page (/skills): the list of Skills (Mine = private to one agent, Shared, Templates), search, creator filter, archived toggle, the "+ Create" menu
(describe with AI -> 12.87 draft, write manually with a live quality check, import a zip or a folder), row menu (edit, share, archive, open in Context Hub).
English source strings; Vietnamese through i18n_dict.json. APIs: app/skills_mgmt.py (/api/skills*). Plain textarea editor (EasyMDE -> GĐ15). Read-only for viewers and the demo account."""
from ui_ls3 import P

CSS = """<style>
.skg{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:start}.skg textarea{font-family:var(--mono);font-size:12.5px;width:100%}
.skl{display:grid;gap:6px;font-size:12.5px}.skl .e{color:var(--er,#b91c1c)}.skl .w{color:var(--t3)}.skl .ok{color:var(--ok,#047857)}
.skm{display:grid;gap:2px;padding:6px;min-width:190px}.skm a,.skm button{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:var(--r-sm);color:var(--t1);background:none;border:0;cursor:pointer;text-align:left;text-decoration:none;height:auto}
.skm a:hover,.skm button:hover{background:var(--s3);text-decoration:none}@media(max-width:900px){.skg{grid-template-columns:1fr}}
</style>"""

SKILLS = P("skills", "Skills", CSS + """<div class="ph" id="h"></div><div class="bodyx" id="b"></div>""", r"""
let TAB=QS.get('tab')||'mine',AG=[],AGENT=QS.get('agent')||'',Q='',CR='',ARCH=false,ROWS=[],CREATORS=[];
const RO=()=>!!window.AOS_RO;
const lm=i=>(window.LANG||'en')==='vi'?i.msg:(i.msg_en||i.msg);
const agName=id=>(AG.find(a=>a.id===id)||{}).name||('#'+id);
const mdOf=(n,d,b)=>`---\nname: ${n}\ndescription: ${JSON.stringify((d||'').replace(/\s+/g,' ').trim())}\n---\n\n${(b||'').trim()}\n`;
const slug=s=>s.replace(/đ/g,'d').replace(/Đ/g,'D').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,64).replace(/-+$/,'');
const agOpts=(sel,first)=>`<option value="">${first}</option>`+AG.map(a=>`<option value="${a.id}" ${String(a.id)===String(sel)?'selected':''}>${esc(a.name)}</option>`).join('');
const lintHtml=L=>!L?'':(L.errors.length||L.warnings.length)?`<div class="skl">${L.errors.map(i=>`<div class="e">• ${esc(lm(i))}</div>`).join('')}${L.warnings.map(i=>`<div class="w">• ${esc(lm(i))}</div>`).join('')}</div>`:`<div class="skl"><div class="ok">${ICN('check')} No problems found</div></div>`;

async function load(){
 const qs=new URLSearchParams({scope:TAB==='mine'?'private':TAB==='shared'?'shared':'template',q:Q});
 if(TAB==='mine'&&AGENT)qs.set('agent_id',AGENT);if(ARCH&&TAB!=='template')qs.set('archived','true');
 ROWS=await api('api/skills?'+qs);
 if(TAB!=='template')CREATORS=[...new Set([...CREATORS,...ROWS.map(r=>r.created_by).filter(Boolean)])].sort();
 draw()}

function draw(){
 const keep=document.activeElement&&document.activeElement.id==='sq';
 $('h').innerHTML=hdr('sparkles','Skills','',`<input id="sq" placeholder="Search skills..." style="width:200px" value="${esc(Q)}">`
  +(TAB==='template'?'':`<select id="scr" title="Creator"><option value="">All creators</option>${CREATORS.map(c=>`<option ${c===CR?'selected':''}>${esc(c)}</option>`).join('')}</select><label class="row" style="gap:4px;margin:0"><input type="checkbox" id="sarch" ${ARCH?'checked':''}> Archived</label>`)
  +(RO()?'':`<button id="snew" data-menu="1">${ICN('plus')}Create</button>`));
 const tabs=`<div class="tabs">${[['mine','Mine'],['shared','Shared'],['template','Templates']].map(([k,l])=>`<button data-t="${k}" class="${k===TAB?'on':''}">${l}</button>`).join('')}</div>`;
 const ags=TAB==='shared'?'':`<div class="row" style="margin-bottom:10px"><label style="margin:0">Agent</label><select id="sag">${agOpts(AGENT,TAB==='mine'?'All my agents':'Pick an agent')}</select><small class="muted">${TAB==='mine'?'Skills private to one agent.':'The agent that will use the template.'}</small></div>`;
 const rows=ROWS.filter(r=>TAB==='template'||!CR||r.created_by===CR);
 let tbl;
 if(TAB==='template')tbl=rows.length?`<div class="card" style="padding:0"><table><thead><tr><th>Template</th><th>Review</th><th></th></tr></thead><tbody>${rows.map(t=>`<tr><td><b>${esc(t.name)}</b><div class="muted" style="font-size:12px">${esc(t.description||'')}</div></td><td>${t.review?'<span class="chip wn">Needs an expert review</span>':'<span class="muted">–</span>'}</td><td style="white-space:nowrap"><button class="g s" data-pv="${esc(t.name)}">${ICN('eye')}Preview</button> ${RO()?'':`<button class="s" data-use="${esc(t.name)}">Use for agent</button>`}</td></tr>`).join('')}</tbody></table></div>`:emptyState('layers','No templates found','Try another search.');
 else tbl=rows.length?`<div class="card" style="padding:0"><table><thead><tr><th>Skill</th><th>Who can use it</th><th>Created by</th><th>Source</th><th>Updated</th><th></th></tr></thead><tbody>${rows.map(r=>`<tr class="r" data-i="${r.id}"><td><b>${esc(r.name)}</b>${r.archived?' <span class="chip">Archived</span>':''}<div class="muted trunc" style="font-size:12px;max-width:420px">${esc(r.description||'')}</div></td><td>${r.visibility==='private'?`<span class="chip pu">Private</span> <small class="muted">${r.owner_agent_id?esc(agName(r.owner_agent_id)):''}</small>`:'<span class="chip ok">Shared</span>'}</td><td>${esc(r.created_by||'–')}</td><td>${r.source?`<span class="chip">${esc(r.source)}</span>`:'<span class="muted">–</span>'}</td><td>${rel(r.updated||r.created_at)}</td><td><button class="gh" data-m="${r.id}" data-menu="1" title="More">${ICN('ellipsis')}</button></td></tr>`).join('')}</tbody></table></div>`
  :emptyState('sparkles',TAB==='mine'?'No private skills yet':'No shared skills yet','A Skill is a short set of steps an agent follows for one kind of job. Create one with AI, write it, import it, or start from a template.',RO()?'':'<button id="snew2" data-menu="1">Create a skill</button>');
 $('b').innerHTML=tabs+ags+tbl;
 if(keep){const e=$('sq');e.focus();e.setSelectionRange(e.value.length,e.value.length)}
 wire()}

function wire(){
 let tm;$('sq').oninput=e=>{Q=e.target.value;clearTimeout(tm);tm=setTimeout(()=>load().catch(x=>flash(x.message,1)),300)};
 if($('scr'))$('scr').onchange=e=>{CR=e.target.value;draw()};
 if($('sarch'))$('sarch').onchange=e=>{ARCH=e.target.checked;load().catch(x=>flash(x.message,1))};
 if($('sag'))$('sag').onchange=e=>{AGENT=e.target.value;load().catch(x=>flash(x.message,1))};
 if($('snew'))$('snew').onclick=e=>createMenu(e.currentTarget);if($('snew2'))$('snew2').onclick=e=>createMenu(e.currentTarget);
 $('b').querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>{TAB=b.dataset.t;CR='';ARCH=false;load().catch(x=>flash(x.message,1))});
 $('b').querySelectorAll('tr[data-i]').forEach(r=>r.onclick=e=>{if(e.target.closest('[data-m]'))return;openSkill(+r.dataset.i)});
 $('b').querySelectorAll('[data-m]').forEach(b=>b.onclick=e=>{e.stopPropagation();rowMenu(b,+b.dataset.m)});
 $('b').querySelectorAll('[data-pv]').forEach(b=>b.onclick=()=>previewTpl(b.dataset.pv));
 $('b').querySelectorAll('[data-use]').forEach(b=>b.onclick=()=>useTpl(b.dataset.use))}

function createMenu(anchor){
 const m=aosPopup(anchor,`<div class="skm"><button id="cai">${ICN('sparkles')}Describe with AI</button><button id="cman">${ICN('file-text')}Write manually</button><button id="cimp">${ICN('upload')}Import zip or folder</button></div>`,{right:true});
 m.querySelector('#cai').onclick=()=>{m.remove();aiPane()};m.querySelector('#cman').onclick=()=>{m.remove();editor({name:'',description:'',body:'',files:{},agent_id:AGENT&&TAB==='mine'?+AGENT:null,source:'manual'})};m.querySelector('#cimp').onclick=()=>{m.remove();importer()}}

function rowMenu(anchor,id){
 const r=ROWS.find(x=>x.id===id);if(!r)return;
 const own=r.can_edit&&!RO()&&!r.archived;
 const m=aosPopup(anchor,`<div class="skm"><button data-a="open">${ICN('file-text')}${own?'Edit':'Open'}</button>${own&&r.visibility==='private'?`<button data-a="share">${ICN('share-2')}Share with the workspace</button>`:''}${own?`<button data-a="arch">${ICN('x')}Archive</button>`:''}<a href="context?pack=${r.id}">${ICN('library')}Open in Context Hub</a></div>`,{right:true});
 m.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{m.remove();const a=b.dataset.a;
  try{if(a==='open')openSkill(id);
   else if(a==='share'){if(!confirm('Share this skill with everyone in the workspace?'))return;await api(`api/skills/${id}/share`,'POST',{visibility:'workspace'});flash('Skill shared');load()}
   else if(a==='arch'){if(!confirm('Archive this skill? Agents already using it keep working.'))return;await api('api/skills/'+id,'DELETE');flash('Skill archived');load()}}
  catch(e){flash(e.message,1)}})}

async function openSkill(id){
 try{const r=await api('api/skills/'+id);const p=r.parsed||{};
  editor({id,name:p.name||r.skill.name,description:p.description||'',body:p.body||'',files:r.files,agent_id:r.skill.owner_agent_id,can_edit:r.can_edit,archived:r.skill.archived,legacy:r.skill.legacy_context_id!=null,source:r.skill.source})}
 catch(e){flash(e.message,1)}}

function editor(init){
 const edit=!!init.id,extra=Object.keys(init.files||{}).filter(f=>f!=='SKILL.md');
 const ro=RO()||init.archived||init.legacy||(edit&&!init.can_edit);
 pane(edit?'Edit skill':'New skill',`<div class="skg"><div style="display:grid;gap:8px">
  <label style="margin:0">Name <small class="muted">(lowercase letters, digits, hyphens)</small></label><input id="snm" value="${esc(init.name)}" ${edit||ro?'disabled':''} maxlength="64" style="font-family:var(--mono)">
  <label style="margin:0">When to use it <small class="muted">(start with "Use when…" / "Dùng khi…")</small></label><textarea id="sds" rows="3" maxlength="1024" ${ro?'disabled':''} style="font-family:var(--font)">${esc(init.description)}</textarea>
  <label style="margin:0">Steps</label><textarea id="sbd" rows="16" spellcheck="false" ${ro?'disabled':''}>${esc(init.body)}</textarea>
  <label style="margin:0">Used by</label><select id="sagt" ${edit||ro?'disabled':''}>${agOpts(init.agent_id,'Everyone in the workspace (shared)')}</select>
  ${extra.length?`<div class="row" style="gap:6px"><small class="muted">Other files kept as they are:</small>${extra.map(f=>`<span class="chip mono">${esc(f)}</span>`).join(' ')}<small class="muted">Edit them in the Context Hub.</small></div>`:''}
  ${init.legacy?'<small class="muted">This skill comes from the older Skills page; it is read-only here.</small>':init.archived?'<small class="muted">Archived skills are read-only.</small>':''}
  <div class="row" style="margin-top:6px"><span class="sp"></span><button class="g" onclick="closePane()">Cancel</button>${ro?'':'<button id="ssave" disabled>Save</button>'}</div></div>
  <div><div class="card" style="padding:12px"><div class="h4" style="margin-bottom:6px">Quality check</div><div id="slint" class="muted">Checking…</div></div>
  ${edit&&!RO()?`<div class="card" style="padding:12px;margin-top:12px"><div class="h4" style="margin-bottom:6px">Try it</div><textarea id="try-p" rows="2" placeholder="Type a request the agent might get, then press Try" style="font-family:var(--font)"></textarea>
   <div class="row" style="margin-top:6px"><span class="sp"></span><button class="g" id="try-go">${ICN('play')}Try</button></div><div id="try-r"></div>
   <details style="margin-top:8px"><summary style="cursor:pointer;font-size:13px">Test when it is used</summary><label>Should use the Skill (one request per line, up to 5)</label><textarea id="try-s" rows="3" style="font-family:var(--font)"></textarea>
   <label>Should NOT use it (one request per line, up to 5)</label><textarea id="try-n" rows="3" style="font-family:var(--font)"></textarea><div class="row" style="margin-top:6px"><span class="sp"></span><button class="g" id="try-act">Run the test</button></div><div id="try-ar"></div></details></div>`:''}</div></div>`,p=>{
  const nm=p.querySelector('#snm'),ds=p.querySelector('#sds'),bd=p.querySelector('#sbd'),ag=p.querySelector('#sagt'),sv=p.querySelector('#ssave');
  p.style.width='min(980px,96vw)';
  if(!edit&&!ro)nm.onblur=()=>{nm.value=slug(nm.value)};
  const files=()=>({...(init.files||{}),'SKILL.md':mdOf(nm.value,ds.value,bd.value)});
  let L=null,tm,seq=0;
  const check=async()=>{const my=++seq;let r;
   try{r=await api('api/skills/lint','POST',{name:nm.value||'x',files:files(),agent_id:ag.value?+ag.value:null})}catch(e){r={errors:[{code:'',msg:e.message,msg_en:e.message}],warnings:[]}}
   if(my!==seq)return;L=r;p.querySelector('#slint').innerHTML=lintHtml(L);p.querySelector('#slint').classList.remove('muted');
   if(sv)sv.disabled=L.errors.length>0||!nm.value||!ds.value.trim()||!bd.value.trim()};
  [nm,ds,bd,ag].forEach(e=>e.oninput=e.onchange=()=>{clearTimeout(tm);tm=setTimeout(check,400)});
  check();
  if(p.querySelector('#try-go')){
   const body=x=>({agent_id:init.agent_id||(AGENT?+AGENT:null),...x}),lines=id=>p.querySelector(id).value.split('\n').map(x=>x.trim()).filter(Boolean);
   p.querySelector('#try-go').onclick=async()=>{const q=p.querySelector('#try-p').value.trim();if(!q)return flash('Type a request to try first',1);
    const b=p.querySelector('#try-go'),out=p.querySelector('#try-r');b.disabled=true;out.innerHTML=`<div class="muted" style="margin-top:8px">${ICN('loader')} Running…</div>`;
    try{const r=await api(`api/skills/${init.id}/try`,'POST',body({prompt:q}));
     out.innerHTML=`<div style="margin-top:8px"><span class="chip ${r.used_skill?'ok':'wn'}" data-used="${r.used_skill?1:0}">${r.used_skill?'Skill used':'Skill not used'}</span> <small class="muted mono">${r.reads.map(esc).join(', ')}</small></div><div class="card" style="padding:8px;margin-top:6px;white-space:pre-wrap;font-size:13px">${esc(r.answer)}</div>`}
    catch(e){out.innerHTML='';flash(e.message,1)}finally{b.disabled=false}};
   p.querySelector('#try-act').onclick=async()=>{const sh=lines('#try-s'),sn=lines('#try-n'),n=sh.length+sn.length;if(!n)return flash('Type at least one request to test',1);if(sh.length>5||sn.length>5)return flash('At most 5 requests in each box',1);
    if(!confirm(`This runs the model ${n} time(s) and costs tokens. Continue?`))return;
    const b=p.querySelector('#try-act'),out=p.querySelector('#try-ar');b.disabled=true;out.innerHTML=`<div class="muted" style="margin-top:8px">${ICN('loader')} Running…</div>`;
    try{const r=await api(`api/skills/${init.id}/try`,'POST',body({activation:{should:sh,shouldnt:sn},confirm_cost:true}));
     out.innerHTML=`<div style="margin-top:8px"><span class="chip ${r.hits===r.should_total?'ok':'wn'}">Used when it should: ${r.hits}/${r.should_total}</span> <span class="chip ${r.false_positives?'wn':'ok'}">Used when it should not: ${r.false_positives}/${r.shouldnt_total}</span></div>${r.results.map(x=>`<div class="row" style="gap:6px;margin-top:4px;flex-wrap:nowrap"><span class="chip ${x.ok?'ok':'er'}">${x.ok?'OK':'Wrong'}</span><span style="font-size:12.5px">${esc(x.prompt)}</span></div>`).join('')}`}
    catch(e){out.innerHTML='';flash(e.message,1)}finally{b.disabled=false}}}
  if(sv)sv.onclick=async()=>{sv.disabled=true;
   try{if(edit)await api('api/skills/'+init.id,'PUT',{files:files(),message:'Edit skill'});
    else await api('api/skills','POST',{name:nm.value,files:files(),agent_id:ag.value?+ag.value:null,visibility:ag.value?'private':'workspace',source:init.source||'manual'});
    closePane();flash('Skill saved');if(!edit){TAB=ag.value?'mine':'shared';if(ag.value)AGENT=ag.value}load()}
   catch(e){sv.disabled=false;flash(e.message,1)}}})}

function aiPane(){
 pane('Describe with AI',`<div style="display:grid;gap:8px"><div class="muted" style="font-size:13px">Say in plain words what the agent should do and when. The AI writes a draft; you check it before saving. Nothing is saved yet.</div>
  <textarea id="aid" rows="7" placeholder="For example: when a customer asks for a quote, ask for the product and quantity, check the price list, and say the price excludes VAT." style="font-family:var(--font)"></textarea>
  <label style="margin:0">Used by</label><select id="aia">${agOpts(AGENT,'Everyone in the workspace (shared)')}</select>
  <div class="row"><span class="sp"></span><button class="g" onclick="closePane()">Cancel</button><button id="aigo">${ICN('sparkles')}Write a draft</button></div><div id="aist"></div></div>`,p=>{
  p.querySelector('#aigo').onclick=async()=>{const t=p.querySelector('#aid').value.trim(),a=p.querySelector('#aia').value;if(t.length<5)return flash('Describe the job first',1);
   const b=p.querySelector('#aigo');b.disabled=true;p.querySelector('#aist').innerHTML=`<div class="muted">${ICN('loader')} Writing a draft…</div>`;
   try{const r=await api('api/skills/draft','POST',{mode:'describe',text:t,agent_id:a?+a:null});const d=r.draft;
    editor({name:d.name,description:d.description,body:d.body,files:d.files,agent_id:a?+a:null,source:'ai'})}
   catch(e){b.disabled=false;p.querySelector('#aist').innerHTML='';flash(e.message,1)}}})}

function importer(){
 let last=null;
 pane('Import skills',`<div style="display:grid;gap:8px"><div class="muted" style="font-size:13px">Choose a zip file or a folder. Each skill folder needs a SKILL.md. Text files only. Skills with errors are skipped.</div>
  <label style="margin:0">Zip file</label><input type="file" id="izip" accept=".zip"><label style="margin:0">or a folder</label><input type="file" id="idir" webkitdirectory multiple>
  <label style="margin:0">Used by</label><select id="iag">${agOpts(AGENT,'Everyone in the workspace (shared)')}</select>
  <div class="row"><span class="sp"></span><button class="g" onclick="closePane()">Cancel</button><button class="g" id="ichk">Check</button><button id="igo" disabled>Import</button></div><div id="ires"></div></div>`,p=>{
  const payload=async()=>{const z=p.querySelector('#izip').files[0],d=[...p.querySelector('#idir').files];
   if(z){const fd=new FormData();fd.append('file',z);return fd}
   if(d.length){if(d.length>500)throw new Error('Too many files (max 500)');const files={};for(const f of d)files[f.webkitRelativePath||f.name]=f.size>2*1024*1024?'\u0000':await f.text();return {files}}
   throw new Error('Choose a zip file or a folder')};
  const show=(r,saved)=>{p.querySelector('#ires').innerHTML=Object.entries(r.skills).map(([n,v])=>{const L=v.lint||(v.detail&&v.detail.lint)||v;const ok=saved?v.saved:v.ok;
    return `<div class="card" style="padding:10px;margin-top:8px"><b>${esc(n)}</b> ${ok?`<span class="chip ok">${saved?'Saved':'Ready'}</span>`:`<span class="chip er">${saved&&v.status===409?'Name already used':'Has errors'}</span>`}${lintHtml(L&&L.errors?L:null)}</div>`}).join('')};
  p.querySelector('#ichk').onclick=async()=>{try{last=await payload();const r=await api('api/skills/import/lint','POST',last);show(r,false);p.querySelector('#igo').disabled=!Object.values(r.skills).some(v=>v.ok)}catch(e){flash(e.message,1)}};
  p.querySelector('#igo').onclick=async()=>{const a=p.querySelector('#iag').value;try{last=last||await payload();const r=await api('api/skills/import/save'+(a?'?agent_id='+a:''),'POST',last);show(r,true);flash(r.saved+' skill(s) imported');p.querySelector('#igo').disabled=true;if(r.saved){TAB=a?'mine':'shared';if(a)AGENT=a;load()}}catch(e){flash(e.message,1)}}})}

async function previewTpl(name){
 try{const r=await api('api/skills/template/'+encodeURIComponent(name));
  pane(r.template.name,`<div class="muted" style="margin-bottom:8px">${esc(r.template.description||'')}</div>${r.template.review?'<div class="chip wn" style="margin-bottom:8px">Needs an expert review before official use</div>':''}<pre style="white-space:pre-wrap;font-size:12.5px;margin:0">${esc(r.files['SKILL.md']||'')}</pre>`)}
 catch(e){flash(e.message,1)}}
async function useTpl(name){
 if(!AGENT)return flash('Pick an agent first',1);
 try{await api('api/skills/from-template','POST',{template:name,agent_id:+AGENT});flash('Template copied as a private skill');TAB='mine';load()}catch(e){flash(e.message,1)}}

(async()=>{try{AG=await api('api/agents');AG=Array.isArray(AG)?AG:(AG.agents||[]);if(!['mine','shared','template'].includes(TAB))TAB='mine';await load()}catch(e){flash(e.message,1)}})();
""")

PAGES = {"/skills": SKILLS}
