"""GĐ9.Q: JS shared by several pages (no imports, so ui_ls3 and ui_parity can both use it)."""

IDENTITY_JS = r"""
async function identityTab(id,b){
 const d=await api('api/agents/'+id+'/identity').catch(e=>({error:e.message}));
 if(d.error){b.innerHTML=`<div class="card"><div class="muted">${esc(d.error)}</div></div>`;return}
 const A=(await api('api/agents/'+id+'/audit?limit=60').catch(()=>({audit:[]}))).audit;
 const mode=d.mode;
 b.innerHTML=`<div class="grid2" style="align-items:start"><div class="card">
  <div class="h4">Agent identity</div><div class="muted" style="font-size:12.5px;margin:2px 0 10px">Whose connected accounts (Google, Microsoft, GitHub…) the agent uses when it calls tools.</div>
  <label style="display:flex;gap:8px;align-items:flex-start;margin:8px 0"><input type="radio" name="idm" value="user" ${mode==='user'?'checked':''} ${d.can_change?'':'disabled'}><span><b>User credentials</b><br><span class="muted">The agent acts with the accounts of the person who is chatting. Audit trail = that person. Needs someone present: channels and schedules cannot use it.</span></span></label>
  <label style="display:flex;gap:8px;align-items:flex-start;margin:8px 0"><input type="radio" name="idm" value="agent" ${mode==='agent'?'checked':''} ${d.can_change?'':'disabled'}><span><b>Agent credentials</b><br><span class="muted">The agent always acts with the fixed accounts of its owner.</span>${d.owner?`<span class="muted"> Owner: <b>${esc(d.owner)}</b></span>`:''}<br><span class="muted">Use it for a shared service (team bot, daily briefing). Everyone who can talk to the agent can use those accounts through it.</span></span></label>
  ${d.can_change?`<div class="row" style="margin-top:10px"><button id="idsave">Save identity</button><span class="muted" style="font-size:12px">Every change is written to the audit log.</span></div>`:`<div class="muted" style="margin-top:8px;font-size:12.5px">Only the owner or a workspace admin can change this.</div>`}
 </div><div class="card">
  <div class="h4">Service key</div><div class="muted" style="font-size:12.5px;margin:2px 0 10px">A key that can run this agent and read its threads — nothing else. Send <code>X-On-Behalf-Of: &lt;username&gt;</code> to record whose request you are serving.</div>
  ${d.service_key?`<div class="row"><span class="chip ok">Active</span><code>${esc(d.service_key.prefix)}…</code><span class="muted" style="font-size:12px">${d.service_key.last_used?'last used '+esc(dt(d.service_key.last_used)):'never used'}</span></div>`:`<div class="muted">No service key.</div>`}
  ${d.can_manage_key?`<div class="row" style="margin-top:10px"><button class="g" id="skmk">${d.service_key?'Rotate key':'Create key'}</button>${d.service_key?`<button class="g" id="skrm">Revoke</button>`:''}</div><div id="skout"></div>`:''}
 </div></div>
 <div class="card" style="padding:0;margin-top:14px"><div class="h4" style="padding:12px 14px 0">Audit: who acted as whom</div><table id="audtbl"><thead><tr><th>When</th><th>Event</th><th>Tool</th><th>Acted as</th><th>By</th><th>On behalf of</th><th>Result</th></tr></thead><tbody>${A.map(r=>`<tr><td>${esc(dt(r.ts))}</td><td>${esc(r.action.replace(/_/g,' '))}</td><td class="mono">${esc(r.tool||'')}</td><td>${r.acted_as?`<span class="chip ${r.acted_as==='agent'?'':'ok'}">${esc(r.acted_as)}</span>`:''}</td><td>${esc(r.principal||'system')}</td><td>${esc(r.on_behalf_of||'')}</td><td>${r.ok?'<span class="chip ok">ok</span>':'<span class="chip er">failed</span>'}${r.detail&&r.detail.auth_required?' <span class="chip wn">authorization required</span>':''}</td></tr>`).join('')||`<tr><td colspan="7" class="muted">Nothing recorded yet.</td></tr>`}</tbody></table></div>`;
 const sv=document.getElementById('idsave');
 if(sv)sv.onclick=async()=>{try{await api('api/agents/'+id+'/identity','PUT',{mode:b.querySelector('[name=idm]:checked').value});flash('Identity saved');identityTab(id,b)}catch(e){flash(e.message,1)}};
 const mk=document.getElementById('skmk');
 if(mk)mk.onclick=async()=>{if(d.service_key&&!confirm('Rotate the key? The current key stops working at once.'))return;try{const r=await api('api/agents/'+id+'/service-key','POST');document.getElementById('skout').innerHTML=`<div class="card" style="margin-top:10px"><b>Copy the key now — it is shown only once.</b><pre id="skey" style="user-select:all">${esc(r.key)}</pre><small class="muted">curl -H "X-Api-Key: …" -H "X-On-Behalf-Of: alice" ${esc(location.origin)}/api/v1/…</small></div>`}catch(e){flash(e.message,1)}};
 const rm=document.getElementById('skrm');
 if(rm)rm.onclick=async()=>{if(!confirm('Revoke the service key?'))return;await api('api/agents/'+id+'/service-key','DELETE');flash('Revoked');identityTab(id,b)}}
// GĐ11.62: before a schedule / channel / mail trigger is added to an agent that has Google / Microsoft tools but uses the identity of whoever is chatting.
// Resolves true = go on (identity switched, or kept by choice), false = cancelled. `need` = the trigger cannot work without a fixed identity (mail trigger).
function officeIdGuard(id,tools,need){
 if(!(tools||[]).some(k=>/^(google|ms365):/.test(k)))return Promise.resolve(true);
 return api('api/agents/'+id+'/identity').catch(()=>null).then(d=>{if(!d||d.mode==='agent')return true;
  return new Promise(res=>pane('Use a fixed identity?',`<p>This agent has Google / Microsoft tools and acts with the accounts of the person who is chatting. A trigger starts it with nobody chatting, so it cannot read or write a mailbox or calendar.</p>
   <p>Switch to agent credentials: it then always acts with the fixed accounts of its owner. Everyone who can talk to the agent can use those accounts through it.</p>
   ${d.can_change?'':'<p class="muted">Only the owner or a workspace admin can change this.</p>'}
   <div class="row" style="margin-top:14px"><button class="g" id="oigx">Cancel</button><span class="sp"></span>${need?'':'<button class="g" id="oigk">Keep user identity</button>'}<button id="oigy" ${d.can_change?'':'disabled'}>Switch to fixed identity</button></div>`,p=>{
   p.querySelector('#oigx').onclick=()=>{closePane();res(false)};const k=p.querySelector('#oigk');if(k)k.onclick=()=>{closePane();res(true)};
   p.querySelector('#oigy').onclick=async()=>{try{await api('api/agents/'+id+'/identity','PUT',{mode:'agent'});flash('Identity saved');closePane();res(true)}catch(e){flash(e.message,1)}}}))})}
"""
