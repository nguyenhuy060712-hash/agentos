"""GĐ13.6: agent page tab "Memory" (shared agent layer files + proposals inbox). English source strings; Vietnamese through i18n_dict.json. API: app/memory_admin.py."""

MEMORY_JS = r"""
async function memoryTab(id,b){
 let F,P;
 try{[F,P]=await Promise.all([api('api/agents/'+id+'/memory/files'),api('api/agents/'+id+'/memory/proposals')])}
 catch(e){b.innerHTML=`<div class="card"><div class="muted">${esc(e.message)}</div></div>`;return}
 b.innerHTML=`<div class="card" style="padding:0"><div class="row" style="padding:12px 14px 0"><div class="h4">Shared memory (all conversations of this agent)</div><span class="sp"></span><button class="g sm" id="mnew">${ICN('plus')}New file</button></div>
  <div class="muted" style="font-size:12.5px;padding:2px 14px 8px">Notes the agent keeps in /memories/agent/. Personal notes about each person stay private and are not shown here.</div>
  <table><thead><tr><th>File</th><th>Size</th><th>Modified</th><th></th></tr></thead><tbody>${F.map(f=>`<tr><td class="mono">${esc(f.path)}</td><td>${f.size}</td><td>${f.modified_at?esc(dt(f.modified_at)):''}</td><td style="text-align:right"><button class="g sm" data-me="${esc(f.path)}">Edit</button> <button class="g sm" data-md="${esc(f.path)}">${ICN('trash')}Delete</button></td></tr>`).join('')||'<tr><td colspan="4" class="muted">No shared notes yet.</td></tr>'}</tbody></table></div>
  <div class="card" style="padding:0;margin-top:14px"><div class="h4" style="padding:12px 14px 0">Proposals waiting for you <span class="chip">${P.length}</span></div>
  <div class="muted" style="font-size:12.5px;padding:2px 14px 8px">When a run that may not write shared memory (public chat, API key, schedule) wants to save a note, it appears here. Nothing is saved until you approve.</div>
  <table><tbody>${P.map(x=>`<tr><td class="mono">${esc(x.path)}<div class="muted" style="font-size:12px">${esc(x.source||'')} · ${esc(dt(x.created_at))}</div></td><td><pre style="white-space:pre-wrap;margin:0;max-height:140px;overflow:auto">${esc(x.content)}</pre></td><td style="text-align:right;white-space:nowrap"><button class="sm" data-pa="${x.id}">Approve</button> <button class="g sm" data-pr="${x.id}">Reject</button></td></tr>`).join('')||'<tr><td class="muted">No proposals.</td></tr>'}</tbody></table></div>`;
 const go=async(pr,msg)=>{try{await pr}catch(e){return flash(e.message,1)}if(msg)flash(msg);memoryTab(id,b)};
 const edit=(path,txt)=>pane('Edit memory file',`<label>File name</label><input id="mpath" value="${esc(path)}" style="width:100%" ${path?'readonly':''} placeholder="/ghi_chu.md"><label>Content</label><textarea id="mtxt" style="width:100%;min-height:240px">${esc(txt)}</textarea><div class="row" style="margin-top:12px"><span class="sp"></span><button id="msave">Save</button></div>`,p=>{
  p.querySelector('#msave').onclick=()=>go(api('api/agents/'+id+'/memory/file','PUT',{path:p.querySelector('#mpath').value,content:p.querySelector('#mtxt').value}),'Saved')});
 $('mnew').onclick=()=>edit('','');
 b.querySelectorAll('[data-me]').forEach(x=>x.onclick=async()=>{try{const r=await api('api/agents/'+id+'/memory/file?path='+encodeURIComponent(x.dataset.me));edit(r.path,r.content)}catch(e){flash(e.message,1)}});
 b.querySelectorAll('[data-md]').forEach(x=>x.onclick=()=>{if(confirm('Delete this memory file?'))go(api('api/agents/'+id+'/memory/file?path='+encodeURIComponent(x.dataset.md),'DELETE'),'Deleted')});
 b.querySelectorAll('[data-pa]').forEach(x=>x.onclick=()=>go(api('api/agents/'+id+'/memory/proposals/'+x.dataset.pa+'/approve','POST'),'Saved to memory'));
 b.querySelectorAll('[data-pr]').forEach(x=>x.onclick=()=>go(api('api/agents/'+id+'/memory/proposals/'+x.dataset.pr+'/reject','POST'),'Rejected'))}
"""
