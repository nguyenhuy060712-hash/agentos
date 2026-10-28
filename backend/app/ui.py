"""Giao diện Moza Agent OS: HTML/CSS/JS inline, không CDN."""
import ui_shell
CSS = ui_shell.CSS

JS = r"""const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const usd=v=>v==null?'–':'$'+Number(v).toFixed(6);const vn=t=>t?new Date(t).toLocaleString((window.LANG||'en')==='vi'?'vi-VN':'en-US',{timeZone:'Asia/Ho_Chi_Minh'}):'';
const RO_OK=/(^|\/)api\/(auth\/(lang|logout)|v1\/(assistants|threads)\/search|chat\/threads\/search)/;
async function api(u,m='GET',b){if(window.AOS_RO&&!/^(GET|HEAD)$/i.test(m)&&!RO_OK.test(u)){const t=(window.LANG||'en')==='vi'?'Tài khoản này chỉ được xem, không được thay đổi':'This account is read-only';flash(t,1);throw new Error(t)}const o={method:m,headers:{}};if(b!==undefined){if(b instanceof FormData)o.body=b;else{o.headers['Content-Type']='application/json';o.body=JSON.stringify(b)}}
 const r=await fetch(u,o);if(r.status===401&&!u.includes('auth/login')){location.href='login?next='+encodeURIComponent(location.pathname+location.search);throw new Error('Chưa đăng nhập')}let j;try{j=await r.json()}catch(e){j={}}if(!r.ok){let d=j.detail;if(Array.isArray(d))d=d.map(x=>(x.loc||[]).join('.')+': '+x.msg).join('; ');else if(d&&typeof d==='object')d=d.message||(d.error&&d.error.message)||JSON.stringify(d);throw new Error(d||('HTTP '+r.status))}return j}
function el(t,c,txt){const d=document.createElement(t);if(c)d.className=c;if(txt!=null)d.textContent=txt;return d}
function flash(m,bad){const d=el('div',null,m);d.style.cssText='position:fixed;right:16px;bottom:16px;padding:10px 14px;border-radius:8px;z-index:99;font-size:13px;box-shadow:var(--sh-lg);border:1px solid var(--bd2);background:'+(bad?'var(--er-bg);color:var(--er)':'var(--el);color:var(--t1)');document.body.append(d);setTimeout(()=>d.remove(),3500)}
"""

def page(active, title, body, script):
    import i18n
    return i18n.inject(_page(active, title, body, script))

def _page(active, title, body, script):
    return (f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title} · Moza Agent OS</title><style>{CSS}</style></head><body>{ui_shell.shell(active, title, body)}'
            f'<script>{JS}{ui_shell.SHELL_JS}{script}</script></body></html>')

# ---------------- CHAT ----------------
CHAT = page("chat", "Chat", """
<style>#content{display:flex;flex-direction:column}#log{flex:1;overflow:auto;padding:14px;max-width:1000px;width:100%;margin:auto}
.m{max-width:85%;margin:8px 0;padding:10px 12px;border-radius:10px;white-space:pre-wrap;line-height:1.45}.u{background:#2563eb;color:#fff;margin-left:auto}
.a{background:#fff;border:1px solid #ddd}.e{background:#fee2e2}.meta{font-size:12px;color:#555;margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;white-space:normal}
.meta input{width:150px;padding:3px 6px;font-size:12px}.meta select{padding:2px;font-size:12px}
details{margin-top:6px;font-size:12px;white-space:normal}summary{cursor:pointer;color:#2563eb}.st{border-left:3px solid #93c5fd;padding:3px 8px;margin:3px 0;background:#f8fafc}
.st.todos{border-color:#f59e0b}.st.subagent{border-color:#a855f7}.st.result{border-color:#d1d5db;color:#444}.st.sub_answer{border-color:#a855f7;background:#faf5ff}
.bar{display:flex;gap:8px;padding:8px 14px;background:#fff;border-bottom:1px solid #ddd;align-items:center;flex-wrap:wrap}
form{display:flex;gap:8px;padding:12px;background:#fff;border-top:1px solid #ddd}#q{flex:1;font-size:16px}</style>
<div class="bar">Agent: <select id="ag"></select><small id="agd"></small><span style="flex:1"></span><small id="tid"></small><button class="g" id="new">Cuộc trò chuyện mới</button></div>
<div id="log"></div><form id="f"><input id="q" placeholder="Nhập câu hỏi…" autocomplete="off"><button id="send">Gửi</button></form>""", r"""
const log=$('log'),q=$('q'),send=$('send');let agents=[];
const newId=()=>'web-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8);
const tidKey=()=>'moza_tid_'+$('ag').value;
function tid(){let t=localStorage.getItem(tidKey());if(!t){t=newId();localStorage.setItem(tidKey(),t)}$('tid').textContent='phiên '+t;return t}
function add(t,c){const d=el('div','m '+c,t);log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
function stepHtml(s){const pad='margin-left:'+(s.depth*18)+'px';
 if(s.kind==='todos')return `<div class="st todos" style="${pad}"><svg class="ic"><use href="#i-list"/></svg> <b>Kế hoạch (write_todos)</b><br>${(s.args?.todos||[]).map(t=>`${t.status==='completed'?'☑':t.status==='in_progress'?'▶':'☐'} ${esc(t.content)}`).join('<br>')}</div>`;
 if(s.kind==='subagent')return `<div class="st subagent" style="${pad}"><svg class="ic"><use href="#i-bot"/></svg> <b>Gọi sub-agent: ${esc(s.args?.subagent_type)}</b><br>${esc((s.args?.description||'').slice(0,400))}</div>`;
 if(s.kind==='tool')return `<div class="st" style="${pad}"><svg class="ic"><use href="#i-wrench"/></svg> <b>${esc(s.name)}</b> ${esc(JSON.stringify(s.args))}${s.depth?' <span class="tag">trong sub-agent</span>':''}</div>`;
 if(s.kind==='sub_answer')return `<div class="st sub_answer" style="${pad}">↩ Sub-agent trả về: ${esc(s.text.slice(0,600))}</div>`;
 return `<div class="st result" style="${pad}">→ ${esc(s.name)}: ${esc((s.text||'').slice(0,300))}</div>`}
function scoreBar(box,j){const m=el('div','meta');
 m.append(el('span',null,`#${j.run_id} · ${j.agent_name||'agent '+j.agent_id} v${j.agent_version} · ${j.tokens} token · ${usd(j.cost_usd)} · ${(j.latency_ms/1000).toFixed(1)}s${j.models_used&&j.models_used.length?' · '+j.models_used.join(' → '):''}`));const tl=el('a',null,'trace');tl.href='traces?run='+j.run_id;m.append(tl);
 const st=el('span');let thumb=null;const up=el('button','s'),dn=el('button','s');up.innerHTML=ICN('thumbs-up');dn.innerHTML=ICN('thumbs-down');
 up.onclick=()=>{thumb=1;up.style.background='#bbf7d0';dn.style.background=''};dn.onclick=()=>{thumb=-1;dn.style.background='#fecaca';up.style.background=''};
 const sel=el('select');sel.append(el('option',null,'điểm'));for(let i=1;i<=5;i++){const o=el('option',null,String(i));o.value=i;sel.append(o)}
 const cm=el('input');cm.placeholder='nhận xét…';const sv=el('button','s','Lưu');
 sv.onclick=async()=>{try{await api('score','POST',{run_id:j.run_id,thumb,score:sel.value==='điểm'?null:+sel.value,comment:cm.value||null});st.textContent='✔ đã lưu'}catch(e){st.textContent='Lỗi: '+e.message}};
 const ai=el('button','s','AI chấm');ai.onclick=async()=>{ai.disabled=true;st.textContent='AI đang chấm…';try{const r=await api('judge','POST',{run_id:j.run_id});st.textContent=`AI: ${r.score}/5 – ${r.reason}`}catch(e){st.textContent='Lỗi: '+e.message}ai.disabled=false};
 m.append(up,dn,sel,cm,sv,ai,st);box.append(m)}
async function loadAgents(){agents=await api('api/agents');const s=$('ag');s.innerHTML=agents.map(a=>`<option value="${a.id}">${esc(a.name)} (v${a.current_version})</option>`).join('');
 const want=new URLSearchParams(location.search).get('agent')||localStorage.getItem('moza_agent');if(want&&agents.some(a=>a.id==want))s.value=want;onAgent()}
function onAgent(){localStorage.setItem('moza_agent',$('ag').value);const a=agents.find(x=>x.id==$('ag').value);$('agd').textContent=a?a.description||'':'';tid();log.innerHTML='';add('Đang nói chuyện với: '+(a?a.name:'?'),'a')}
$('ag').onchange=onAgent;
$('new').onclick=()=>{localStorage.setItem(tidKey(),newId());tid();log.innerHTML='';add('Đã bắt đầu cuộc trò chuyện mới.','a')};
$('f').onsubmit=async e=>{e.preventDefault();const t=q.value.trim();if(!t)return;add(t,'u');q.value='';send.disabled=true;
 const w=add('','a');const ans=el('div',null,'Đang suy nghĩ…');const det=el('details');det.open=true;const sum=el('summary',null,'Các bước (0)');const steps=el('div');det.append(sum,steps);w.append(ans,det);let n=0;
 try{const r=await fetch('chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({thread_id:tid(),message:t,agent_id:+$('ag').value})});
  if(!r.ok)throw new Error('HTTP '+r.status);const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
  while(true){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);if(!line.trim())continue;const ev=JSON.parse(line);
    if(ev.type==='interrupt'){steps.insertAdjacentHTML('beforeend',`<div class="st" style="background:#fef3c7"><svg class="ic"><use href="#i-pause"/></svg> <b>Cần duyệt trước khi chạy:</b> ${esc(JSON.stringify((ev.requests||[]).flatMap(r=>r.action_requests||[]).map(x=>({tool:x.name,args:x.args}))))} — <a href="approvals?id=${ev.approval_id}" target="_blank">mở trang Duyệt #${ev.approval_id}</a></div>`);steps.open=true}
    else if(ev.type==='token'){ans.textContent=(ans.dataset.tk=(ans.dataset.tk||'')+ev.text);log.scrollTop=log.scrollHeight}
    else if(ev.type==='step'){n++;steps.insertAdjacentHTML('beforeend',stepHtml(ev));sum.textContent=`Các bước (${n})`;log.scrollTop=log.scrollHeight}
    else if(ev.type==='done'){if(ev.error){w.className='m e';ans.textContent=(window.t||String)('Error: ')+ev.error}else ans.textContent=ev.reply||(ev.approval_id?(window.t||String)('Waiting for approval')+' (#'+ev.approval_id+')':(window.t||String)('(no reply)'));
     det.open=false;if(!n)det.remove();ev.agent_name=(agents.find(a=>a.id==ev.agent_id)||{}).name;scoreBar(w,ev)}}}}
 catch(err){w.className='m e';ans.textContent='Lỗi: '+err.message}finally{send.disabled=false;q.focus()}};
loadAgents();""")

# ---------------- AGENTS ----------------
AGENTS = page("agents", "Agents", """
<main><div class="card"><div class="row"><h2 style="margin:0">Agents</h2><span style="flex:1"></span><button class="s" id="arch">Đã lưu trữ</button><button id="nb">+ Tạo agent</button></div>
<table id="list"></table></div><div class="card" id="ed" style="display:none"></div></main>""", r"""
let models=[],catalog=[],cur=null;
async function list(){const a=await api('api/agents');$('list').innerHTML='<tr><th>#</th><th>Tên</th><th>Mô tả</th><th>Model</th><th>Phiên bản</th><th></th></tr>'+
 a.map(x=>`<tr><td>${x.id}</td><td><b>${esc(x.name)}</b></td><td>${esc(x.description)}</td><td>${esc(x.model)}</td><td>v${x.current_version} (${x.n_versions})</td>
 <td class="row"><button class="s" onclick="edit(${x.id})">Sửa</button><button class="s" onclick="clone(${x.id})">Nhân bản</button>
 <a href="./?agent=${x.id}"><button class="s">Chat</button></a><button class="s" onclick="delA(${x.id},'${esc(x.name).replace(/'/g,'')}')">Lưu trữ</button></td></tr>`).join('')}
const blank={sandbox:false,fallback_models:['gemini-3-flash-preview','gemini-3.1-flash-lite'],rpm_limit:10,budget_usd_day:null,name:'',description:'',system_prompt:'',model:'gemini-3.6-flash',temperature:0.3,tools:[],subagents:[],planning:true,filesystem:true,long_term_memory:false};
function toolBoxes(sel,prefix){return catalog.map(t=>`<label style="display:inline-block;margin-right:12px" title="${esc(t.description)}"><input type="checkbox" class="${prefix}" value="${esc(t.key)}" ${sel.includes(t.key)?'checked':''} ${t.enabled?'':'disabled'}> ${esc(t.key)}${t.enabled?'':' <span class="tag off">tắt ở Tools/MCP</span>'}</label>`).join('')}
function subHtml(s,i){return `<div class="card sub" style="background:#faf5ff;margin-top:8px"><div class="grid2"><div><label>Tên (a-z, 0-9, -)</label><input class="sn" value="${esc(s.name)}"></div>
 <div><label>Mô tả (agent chính dùng để quyết định giao việc)</label><input class="sd" style="width:100%" value="${esc(s.description)}"></div></div>
 <label>Prompt</label><textarea class="sp" rows="3">${esc(s.system_prompt)}</textarea><label>Tools</label>${toolBoxes(s.tools||[],'st'+i)}
 <div><button class="s" onclick="this.closest('.sub').remove()">Xoá sub-agent</button></div></div>`}
function render(id,c,versions){cur=id;const e=$('ed');e.style.display='block';
 e.innerHTML=`<h3>${id?'Sửa agent #'+id:'Tạo agent mới'}</h3><div class="grid2"><div><label>Tên</label><input id="f_name" style="width:100%" value="${esc(c.name)}"></div>
 <div><label>Mô tả</label><input id="f_desc" style="width:100%" value="${esc(c.description)}"></div></div>
 <label>System prompt</label><textarea id="f_prompt" rows="6">${esc(c.system_prompt)}</textarea>
 <div class="row"><div><label>Model (chỉ Gemini)</label><select id="f_model">${models.map(m=>`<option ${m===c.model?'selected':''}>${esc(m)}</option>`).join('')}</select></div>
 <div><label>Temperature</label><input id="f_temp" type="number" min="0" max="2" step="0.1" value="${c.temperature}" style="width:90px"></div>
 <label><input type="checkbox" id="f_plan" ${c.planning?'checked':''}> Lập kế hoạch (write_todos)</label>
 <label><input type="checkbox" id="f_fs" ${c.filesystem?'checked':''}> Tool filesystem (ls/read/write/edit… – bộ nhớ tạm theo luồng)</label>
 <label><input type="checkbox" id="f_mem" ${c.long_term_memory?'checked':''}> Bộ nhớ dài hạn (Postgres Store)</label>
 <label><input type="checkbox" id="f_sbx" ${c.sandbox?'checked':''}> Sandbox chạy code (Docker, không mạng)</label></div>
 <div class="row"><div><label>Gateway: model dự phòng (theo thứ tự)</label>${models.filter(m=>m!==c.model).slice(0,40).map(m=>`<label style="display:inline-block;margin-right:10px"><input type="checkbox" class="fb" value="${esc(m)}" ${(c.fallback_models||[]).includes(m)?'checked':''}> ${esc(m)}</label>`).join('')}</div></div>
 <div class="row"><div><label>Giới hạn yêu cầu/phút</label><input id="f_rpm" type="number" min="1" value="${c.rpm_limit??''}" style="width:90px"></div>
 <div><label>Ngân sách USD/ngày (trống = không giới hạn)</label><input id="f_bud" type="number" min="0" step="0.01" value="${c.budget_usd_day??''}" style="width:120px"></div></div>
 <div class="row"><div><label>Prompt từ Hub (prompt_ref, vd <code>tro_ly:prod</code>; ghép trước system prompt)</label><input id="f_pref" value="${esc(c.prompt_ref||'')}" style="width:260px"></div>
 <div style="flex:1"><label>Cần duyệt trước khi chạy (HITL) — tool ghi MCP luôn tự động cần duyệt</label>${['local:chay_code_python','local:luu_ghi_nho',...(c.tools||[])].filter((v,i,a)=>a.indexOf(v)===i).map(t=>`<label style="display:inline-block;margin-right:10px"><input type="checkbox" class="hitl" value="${esc(t)}" ${(c.interrupt_on||[]).includes(t)?'checked':''}> ${esc(t)}</label>`).join('')}</div></div>
 <label>Tools được bật</label><div>${toolBoxes(c.tools,'mt')}</div>
 <h4 style="margin:14px 0 0">Sub-agents</h4><div id="subs">${c.subagents.map(subHtml).join('')}</div><button class="s" id="addsub">+ Thêm sub-agent</button>
 <label>Ghi chú phiên bản</label><input id="f_note" style="width:100%" placeholder="vd: chỉnh prompt">
 <div class="row" style="margin-top:12px"><button id="save">${id?'Lưu (tạo phiên bản mới)':'Tạo agent'}</button><button class="g" onclick="$('ed').style.display='none'">Đóng</button>
 ${id?`<a href="api/agents/${id}/export" download="agent-${id}.json"><button class="g">Xuất JSON</button></a>`:''}</div>
 ${versions?`<h4>Lịch sử phiên bản</h4><table><tr><th>Phiên bản</th><th>Thời gian</th><th>Ghi chú</th><th>Model</th><th></th></tr>${versions.map(v=>`<tr><td>v${v.version}</td><td>${vn(v.created)}</td><td>${esc(v.note)}</td><td>${esc(v.config.model)}</td><td>${v.version===c._cur?'<b>hiện tại</b>':`<button class="s" onclick="rb(${id},${v.version})">Rollback</button>`}</td></tr>`).join('')}</table>`:''}`;
 let ns=c.subagents.length;$('addsub').onclick=()=>{$('subs').insertAdjacentHTML('beforeend',subHtml({name:'',description:'',system_prompt:'',tools:[]},ns++))};
 $('save').onclick=save;e.scrollIntoView({behavior:'smooth'})}
function collect(){const ck=c=>[...document.querySelectorAll('input.'+c+':checked')].map(x=>x.value);
 return {name:$('f_name').value,description:$('f_desc').value,system_prompt:$('f_prompt').value,model:$('f_model').value,temperature:+$('f_temp').value,
 planning:$('f_plan').checked,sandbox:$('f_sbx').checked,fallback_models:ck('fb'),rpm_limit:$('f_rpm').value?+$('f_rpm').value:null,budget_usd_day:$('f_bud').value?+$('f_bud').value:null,filesystem:$('f_fs').checked,long_term_memory:$('f_mem').checked,tools:ck('mt'),note:$('f_note').value||null,prompt_ref:$('f_pref').value||null,interrupt_on:ck('hitl'),
 subagents:[...document.querySelectorAll('#subs .sub')].map(d=>({name:d.querySelector('.sn').value,description:d.querySelector('.sd').value,system_prompt:d.querySelector('.sp').value,
 tools:[...d.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value)}))}}
async function save(){try{const c=collect();const r=cur?await api('api/agents/'+cur,'PUT',c):await api('api/agents','POST',c);flash(`Đã lưu agent #${r.id} v${r.version}`);await list();edit(r.id)}catch(e){flash(e.message,1)}}
async function edit(id){const d=await api('api/agents/'+id);d.config._cur=d.agent.current_version;render(id,d.config,d.versions)}
async function clone(id){const r=await api('api/agents/'+id+'/clone','POST');flash('Đã nhân bản → #'+r.id);list()}
async function delA(id,n){if(!confirm('Lưu trữ agent "'+n+'"? Có thể khôi phục sau trong mục Đã lưu trữ.'))return;await api('api/agents/'+id,'DELETE');flash('Đã lưu trữ');list();$('ed').style.display='none'}
async function delPerm(id){const d=await api('api/archived-agents').then(a=>a.find(x=>x.id==id));const n=d?d.name:'';if(!n)return;const t=prompt('Xoá vĩnh viễn agent "'+n+'" cùng mọi cuộc trò chuyện, lịch chạy, kết nối và bộ nhớ của nó. Dấu vết (trace) giữ theo thời hạn lưu của dự án; chi phí vẫn nằm trong báo cáo. Không hoàn tác được.\n\nGõ đúng tên agent để xác nhận:');if(t===null)return;if(t!==n){flash('Tên không khớp',1);return}await api('api/agents/'+id+'?mode=permanent&confirm='+encodeURIComponent(n),'DELETE');flash('Đã xoá vĩnh viễn');arch()}
async function restoreA(id){await api('api/agents/'+id+'/restore','POST');flash('Đã khôi phục');list();arch()}
async function arch(){const a=await api('api/archived-agents');const e=$('ed');e.style.display='block';e.innerHTML='<h3>Đã lưu trữ</h3>'+(a.length?'<table>'+a.map(x=>`<tr><td>${x.id}</td><td><b>${esc(x.name)}</b></td><td class="row"><button class="s" onclick="restoreA(${x.id})">Khôi phục</button><button class="s" onclick="delPerm(${x.id})">Xoá vĩnh viễn</button></td></tr>`).join('')+'</table>':'<p>Không có agent nào được lưu trữ.</p>')}
$('arch').onclick=arch;
async function rb(id,v){if(!confirm('Rollback về v'+v+'? (tạo phiên bản mới với cấu hình v'+v+')'))return;const r=await api(`api/agents/${id}/rollback/${v}`,'POST');flash('Đã rollback → v'+r.version);list();edit(id)}
$('nb').onclick=()=>render(null,JSON.parse(JSON.stringify(blank)));
(async()=>{models=(await api('api/models')).models;catalog=await api('api/toolcatalog');list();const e=new URLSearchParams(location.search).get('edit');if(e)edit(+e)})();""")

# ---------------- TOOLS ----------------
TOOLS = page("tools", "Tools/MCP", """
<main><div class="card"><div class="row"><h2 style="margin:0">MCP servers</h2><span style="flex:1"></span><button class="g" id="rf">Làm mới danh sách tool</button></div>
<table id="srv"></table><details style="margin-top:8px"><summary>+ Thêm MCP server</summary><div class="row" style="margin-top:8px">
<input id="s_name" placeholder="tên (a-z0-9-)"><input id="s_url" placeholder="http://127.0.0.1:…/mcp" style="width:300px">
<select id="s_tr"><option>streamable_http</option><option>sse</option></select><input id="s_tok" placeholder="biến môi trường chứa token (tuỳ chọn)">
<button id="s_add">Thêm</button></div><small>Token không nhập trực tiếp: chỉ ghi tên biến môi trường trong /opt/langchain/.env.</small></details></div>
<div class="card"><h2>Tools</h2><p style="font-size:13px"><svg class="ic"><use href="#i-lock"/></svg> <span>Safety: HA write tools are</span> <b>off by default</b>. <span>Any command touching locks, alarms or garage is</span> <b>always blocked on the server</b>, <span>whatever is enabled here.</span>
<br><small id="blk"></small></p><table id="tl"></table></div>
<div class="card" id="tc" style="display:none"></div>
<div class="card"><h2>Sandbox (chạy code)</h2><p style="font-size:13px">Container Docker dùng-một-lần: <b>không mạng</b>, 256 MB RAM, 0.5 CPU, 64 tiến trình, rootfs chỉ đọc, user nobody, timeout 20s. Tắt mặc định; bật riêng từng agent ở trang Agents.</p>
<textarea id="sbc" rows="4">import sys, platform\nprint(sum(i*i for i in range(10**6)), platform.python_version())</textarea><button id="sbr">Chạy thử</button><pre id="sbo"></pre></div></main>""", r"""
$('sbr').onclick=async()=>{$('sbo').textContent='…';try{const r=await api('api/sandbox/run','POST',{code:$('sbc').value});$('sbo').textContent=`(${r.ms} ms)\n`+r.output}catch(e){$('sbo').textContent='Lỗi: '+e.message}};
async function load(){const d=await api('api/mcp');
 $('srv').innerHTML='<tr><th>Tên</th><th>URL</th><th>Transport</th><th>Token</th><th>Trạng thái</th><th></th></tr>'+d.servers.map(s=>`<tr><td><b>${esc(s.name)}</b></td><td>${esc(s.url)}</td><td>${esc(s.transport)}</td><td>${s.token_env?'env: '+esc(s.token_env):'–'}</td><td class="${s.status==='ok'?'ok':'err'}">${esc(s.status)}</td><td>${s.name==='ha'?'':`<button class="s" onclick="delS('${esc(s.name)}')">Xoá</button>`}</td></tr>`).join('');
 $('blk').textContent='Mẫu chặn: '+d.block_pattern+(d.blocked_devices.length?' · thiết bị bị chặn theo tên: '+d.blocked_devices.join(', '):' · (HA hiện không có thiết bị khoá/báo động/gara nào)');
 $('tl').innerHTML='<tr><th>Server</th><th>Tool</th><th>Mô tả</th><th>Loại</th><th>Bật</th><th></th></tr>'+d.tools.map(t=>`<tr><td>${esc(t.server)}</td><td><b>${esc(t.tool)}</b></td><td>${esc(t.description.slice(0,160))}</td>
 <td>${t.read_only?'<span class="tag ro">chỉ đọc</span>':'<span class="tag">ghi</span>'}${t.blocked?'<span class="tag bl">luôn chặn</span>':''}</td>
 <td><input type="checkbox" ${t.enabled?'checked':''} ${t.blocked?'disabled':''} onchange="tog('${esc(t.server)}','${esc(t.tool)}',this.checked,${t.read_only})"></td>
 <td>${t.read_only?`<button class="s" onclick='test(${JSON.stringify(t.key)},${JSON.stringify(JSON.stringify(t.schema))})'>Gọi thử</button>`:''}</td></tr>`).join('')}
async function tog(s,t,on,ro){if(on&&!ro&&!confirm('Bật tool GHI "'+t+'"? Agent sẽ có thể điều khiển thiết bị (trừ khoá/báo động/gara luôn bị chặn).')){load();return}
 await api('api/mcp/toggle','POST',{server:s,tool:t,enabled:on});flash((on?'Đã bật ':'Đã tắt ')+t)}
function test(key,schema){const c=$('tc');c.style.display='block';c.innerHTML=`<h3>Gọi thử ${esc(key)}</h3><small>Schema: ${esc(schema)}</small><label>Tham số (JSON)</label><textarea id="ta" rows="2">{}</textarea><button id="go">Gọi</button><pre id="out"></pre>`;
 $('go').onclick=async()=>{$('out').textContent='…';try{const r=await api('api/mcp/test','POST',{key,args:JSON.parse($('ta').value||'{}')});$('out').textContent=`(${r.ms} ms)\n`+r.result}catch(e){$('out').textContent='Lỗi: '+e.message}};c.scrollIntoView()}
async function delS(n){if(confirm('Xoá server '+n+'?')){await api('api/mcp/servers/'+n,'DELETE');load()}}
$('rf').onclick=async()=>{await api('api/mcp/refresh','POST');flash('Đã làm mới');load()};
$('s_add').onclick=async()=>{try{const r=await api('api/mcp/servers','POST',{name:$('s_name').value,url:$('s_url').value,transport:$('s_tr').value,token_env:$('s_tok').value||null});flash('Đã thêm: '+r.status);load()}catch(e){flash(e.message,1)}};
load();""")

# ---------------- MEMORY ----------------
MEMORY = page("memory", "Bộ nhớ", """
<main><div class="grid2"><div class="card"><h2>Store dài hạn (Postgres Store)</h2>
<div class="row"><select id="ns"></select><input id="sq" placeholder="tìm ngữ nghĩa (để trống = liệt kê)"><button id="sgo">Xem</button></div>
<table id="items"></table><details><summary>+ Thêm mục vào namespace đang chọn</summary><textarea id="stx" rows="2"></textarea><button id="sadd">Thêm</button></details>
<p><small>Namespace của agent: agent/&lt;id&gt;/memories (khi bật "Bộ nhớ dài hạn"). moza/notes: ghi chú cũ.</small></p></div>
<div class="card"><h2>Cơ sở kiến thức (pgvector)</h2><small>Chia đoạn 800 ký tự (chồng 100), nhúng bằng gemini-embedding-2 (768 chiều). Agent dùng qua tool <b>tra_cuu_kien_thuc</b>.</small>
<label>Tiêu đề</label><input id="kt" style="width:100%"><label>Nội dung</label><textarea id="kx" rows="5"></textarea><button id="kadd">Nạp văn bản</button>
<div class="row" style="margin-top:8px"><input type="file" id="kf" accept=".txt,.md"><button id="kup">Tải file .txt/.md</button></div>
<h3 style="margin-top:14px">Tìm thử</h3><div class="row"><input id="kq" style="flex:1" placeholder="câu hỏi"><button id="kgo">Tìm</button></div><div id="kres"></div>
<h3 style="margin-top:14px">Tài liệu</h3><table id="docs"></table></div></div></main>""", r"""
async function nss(){const n=await api('api/store/namespaces');$('ns').innerHTML=n.map(x=>`<option>${esc(x.join('/'))}</option>`).join('')||'<option>agent/1/memories</option>'}
async function items(){const ns=$('ns').value;if(!ns)return;const r=await api('api/store/items?ns='+encodeURIComponent(ns)+'&q_='+encodeURIComponent($('sq').value));
 $('items').innerHTML='<tr><th>Key</th><th>Nội dung</th><th>Điểm</th><th>Cập nhật</th><th></th></tr>'+r.map(i=>`<tr><td>${esc(i.key)}</td><td>${esc(i.value.text??JSON.stringify(i.value))}</td><td>${i.score??''}</td><td>${vn(i.updated)}</td><td><button class="s" onclick="delI('${esc(i.key)}')">Xoá</button></td></tr>`).join('')}
async function delI(k){if(!confirm('Xoá '+k+'?'))return;await api('api/store/items?ns='+encodeURIComponent($('ns').value)+'&key='+encodeURIComponent(k),'DELETE');items()}
async function docs(){const d=await api('api/kb/docs');$('docs').innerHTML='<tr><th>#</th><th>Tiêu đề</th><th>Đoạn</th><th>Ngày</th><th></th></tr>'+d.map(x=>`<tr><td>${x.id}</td><td>${esc(x.title)}</td><td>${x.n_chunks}</td><td>${vn(x.created)}</td><td><button class="s" onclick="delD(${x.id})">Xoá</button></td></tr>`).join('')}
async function delD(id){if(confirm('Xoá tài liệu #'+id+'?')){await api('api/kb/docs/'+id,'DELETE');docs()}}
$('sgo').onclick=items;$('ns').onchange=items;
$('sadd').onclick=async()=>{await api('api/store/items','POST',{ns:$('ns').value,text:$('stx').value});$('stx').value='';items()};
$('kadd').onclick=async()=>{try{const r=await api('api/kb/text','POST',{title:$('kt').value,text:$('kx').value});flash(`Đã nạp ${r.chunks} đoạn`);docs()}catch(e){flash(e.message,1)}};
$('kup').onclick=async()=>{const f=$('kf').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);try{const r=await api('api/kb/upload','POST',fd);flash(`Đã nạp ${r.chunks} đoạn`);docs()}catch(e){flash(e.message,1)}};
$('kgo').onclick=async()=>{const r=await api('api/kb/search?q_='+encodeURIComponent($('kq').value));$('kres').innerHTML=r.map(x=>`<pre><b>${esc(x.title)} #${x.idx} · ${x.score}</b>\n${esc(x.text)}</pre>`).join('')||'Không có kết quả'};
nss().then(items);docs();""")

# ---------------- EVAL ----------------
EVAL = page("eval", "Đánh giá", """
<main><div class="card"><div class="row"><h2 style="margin:0">Tổng quan</h2><select id="sa"><option value="">mọi agent</option></select><small id="price"></small></div>
<div class="kpi" id="kpi" style="margin-top:8px"></div><h4>Theo ngày (giờ VN)</h4><table id="days"></table></div>
<div class="card"><div class="row"><input id="fq" placeholder="tìm câu hỏi / trả lời / thread"><input id="fd" type="date">
<select id="fa"><option value="">mọi agent</option></select><input id="fv" type="number" min="1" placeholder="phiên bản" style="width:100px">
<select id="fs"><option value="">mọi điểm</option><option value="4">≥4</option><option value="3">≥3</option><option value="1">đã chấm</option></select>
<select id="fe"><option value="">mọi trạng thái</option><option value="false">không lỗi</option><option value="true">có lỗi</option></select><button id="go">Lọc</button></div><table id="runs"></table></div>
<div class="card" id="detail" style="display:none"></div>
<div class="card"><div class="row"><h2 style="margin:0">Bộ test</h2><input id="tsn" placeholder="tên bộ test mới"><button id="tsadd">Tạo</button></div><table id="tsl"></table><div id="tsd"></div></div></main>""", r"""
let agents=[];
async function stats(){const a=$('sa').value;const s=await api('api/stats'+(a?'?agent_id='+a:'')),t=s.total,p=s.pricing,ps=s.pricing_source;
 $('price').textContent=p?`· ${s.model}: $${p.input_per_1m}/1M vào, $${p.output_per_1m}/1M ra (${p.note}; ${ps.note} Nguồn ${ps.url}, kiểm ${ps.checked})`:'';
 $('kpi').innerHTML=[['Số run',t.runs],['Tổng token',t.tokens],['Tổng chi phí',usd(t.cost)],['Điểm TB (người)',t.avg_human??'–'],['Điểm TB (AI)',t.avg_ai??'–'],['<svg class="ic"><use href="#i-thumbs-up"/></svg> / <svg class="ic"><use href="#i-thumbs-down"/></svg>',`${t.up} / ${t.down}`],['Lỗi',t.errors],['Độ trễ TB',t.avg_ms?(t.avg_ms/1000).toFixed(1)+'s':'–']].map(([k,v])=>`<div>${k}<b>${esc(v)}</b></div>`).join('');
 $('days').innerHTML='<tr><th>Ngày</th><th>Run</th><th>Token vào</th><th>Token ra</th><th>Tổng</th><th>Chi phí</th><th>Điểm TB</th><th>Trễ TB</th></tr>'+s.days.map(d=>`<tr><td>${d.day}</td><td>${d.runs}</td><td>${d.in_tok}</td><td>${d.out_tok}</td><td>${d.tokens}</td><td>${usd(d.cost)}</td><td>${d.avg_score??'–'}</td><td>${(d.avg_ms/1000).toFixed(1)}s</td></tr>`).join('')}
async function runs(){const p=new URLSearchParams();[['q_','fq'],['day','fd'],['agent_id','fa'],['agent_version','fv'],['min_score','fs'],['has_error','fe']].forEach(([k,i])=>{if($(i).value)p.set(k,$(i).value)});
 const rs=await api('api/runs?'+p);$('runs').innerHTML='<tr><th>#</th><th>Thời gian</th><th>Agent</th><th>Câu hỏi</th><th>Trả lời</th><th>Tool</th><th>Token</th><th>Chi phí</th><th>Trễ</th><th>Người</th><th>AI</th><th><svg class="ic"><use href="#i-thumbs-up"/></svg></th></tr>'+
 rs.map(r=>`<tr class="r" onclick="detail(${r.id})"><td>${r.id}</td><td>${vn(r.ts)}</td><td>${esc(r.agent_name||'–')}${r.agent_version?' v'+r.agent_version:''}</td><td>${esc(r.user_input)}</td><td class="${r.error?'err':''}">${esc(r.error||r.answer)}</td><td>${r.n_tools}</td><td>${r.total_tokens}</td><td>${usd(r.cost_usd)}</td><td>${(r.latency_ms/1000).toFixed(1)}s</td><td>${r.human_score??''}</td><td>${r.ai_score??''}</td><td>${r.thumbs??''}</td></tr>`).join('')}
async function detail(id){const d=await api('api/runs/'+id),r=d.run,box=$('detail');box.style.display='block';
 box.innerHTML=`<h3>Run #${r.id} <a href="traces?run=${r.id}"><button class="s">Xem trace</button></a> <button class="s" id="aij">AI chấm</button> <small id="ais"></small></h3>
 <p>${vn(r.ts)} · thread ${esc(r.thread_id)} · agent #${r.agent_id??'–'} v${r.agent_version??'–'} · ${esc(r.model)} · ${(r.latency_ms/1000).toFixed(2)}s</p>
 <p><b>Token:</b> vào ${r.input_tokens} · ra ${r.output_tokens} · tổng ${r.total_tokens} · <b>Chi phí:</b> ${usd(r.cost_usd)}${d.pricing?` <small>= ${r.input_tokens}/1M × $${d.pricing.input_per_1m} + ${r.output_tokens}/1M × $${d.pricing.output_per_1m} (nguồn ${esc(d.pricing_source.url)}, ${d.pricing_source.checked})</small>`:''}</p>
 <b>Câu hỏi</b><pre>${esc(r.user_input)}</pre><b>Trả lời</b><pre>${esc(r.answer)}</pre>${r.error?`<p class="err">${esc(r.error)}</p>`:''}
 <b>Tool calls (${(r.tool_calls||[]).length})</b>${(r.tool_calls||[]).map(t=>`<pre>${t.depth?'[sub-agent] ':''}<b>${esc(t.name)}</b> ${esc(JSON.stringify(t.args))}\n→ ${esc((t.result||'').slice(0,800))}</pre>`).join('')||'<p>–</p>'}
 ${r.trace?`<details><summary>Trace đầy đủ (${r.trace.length} bước)</summary><pre>${esc(JSON.stringify(r.trace,null,1))}</pre></details>`:''}
 <b>Điểm</b><table><tr><th>Loại</th><th>Thời gian</th><th><svg class="ic"><use href="#i-thumbs-up"/></svg>/<svg class="ic"><use href="#i-thumbs-down"/></svg></th><th>Điểm</th><th>Nhận xét</th></tr>${d.scores.map(s=>`<tr><td>${s.kind==='ai'?'AI':'Người'}</td><td>${vn(s.ts)}</td><td>${s.thumb==1?'<svg class="ic"><use href="#i-thumbs-up"/></svg>':s.thumb==-1?'<svg class="ic"><use href="#i-thumbs-down"/></svg>':''}</td><td>${s.score??''}</td><td>${esc(s.comment)}</td></tr>`).join('')}</table>`;
 $('aij').onclick=async()=>{$('ais').textContent='đang chấm…';try{await api('judge','POST',{run_id:id})}catch(e){flash(e.message,1)}detail(id);runs();stats()};box.scrollIntoView({behavior:'smooth'})}
async function tsl(){const s=await api('api/testsets');$('tsl').innerHTML='<tr><th>#</th><th>Tên</th><th>Số câu</th><th></th></tr>'+s.map(x=>`<tr><td>${x.id}</td><td>${esc(x.name)}</td><td>${x.n}</td><td><button class="s" onclick="tsd(${x.id})">Mở</button> <button class="s" onclick="tsdel(${x.id})">Xoá</button></td></tr>`).join('')}
async function tsdel(id){if(confirm('Xoá bộ test #'+id+'?')){await api('api/testsets/'+id,'DELETE');tsl();$('tsd').innerHTML=''}}
let poll=null;
async function tsd(id){const d=await api('api/testsets/'+id);const cell=(tr,c)=>d.cells.find(x=>x.test_run_id===tr.id&&x.test_case_id===c.id);
 $('tsd').innerHTML=`<h3 style="margin-top:14px">Bộ test #${id}</h3><div class="row"><input id="cq" placeholder="câu hỏi" style="flex:1"><input id="ce" placeholder="đáp án mong đợi" style="flex:1"><button id="cadd">Thêm câu</button></div>
 <div class="row" style="margin-top:8px">Chạy với: <select id="ra">${agents.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select><input id="rv" type="number" min="1" placeholder="phiên bản (trống = hiện tại)" style="width:200px"><button id="rgo">Chạy + AI chấm</button></div>
 <table style="margin-top:8px"><tr><th>Câu hỏi</th><th>Mong đợi</th>${d.test_runs.map(t=>`<th>Lần #${t.id}: ${esc(t.agent_name)} v${t.agent_version}<br><small>${esc(t.status)}</small></th>`).join('')}<th></th></tr>
 ${d.cases.map(c=>`<tr><td>${esc(c.question)}</td><td>${esc(c.expected)}</td>${d.test_runs.map(t=>{const x=cell(t,c);return `<td>${x?`<b class="${x.ai_score>=4?'ok':x.ai_score?'err':''}">${x.ai_score??'?'}/5</b> ${esc((x.error||x.answer||'').slice(0,200))}<br><small>${esc(x.ai_reason||'')}</small> <a href="#" onclick="detail(${x.run_id});return false">#${x.run_id}</a>`:'…'}</td>`}).join('')}<td><button class="s" onclick="cdel(${c.id},${id})">Xoá</button></td></tr>`).join('')}
 <tr><th colspan="2">Điểm TB</th>${d.test_runs.map(t=>{const xs=d.cells.filter(x=>x.test_run_id===t.id&&x.ai_score);return `<th>${xs.length?(xs.reduce((a,x)=>a+x.ai_score,0)/xs.length).toFixed(2):'–'}</th>`}).join('')}<th></th></tr></table>`;
 $('cadd').onclick=async()=>{await api(`api/testsets/${id}/cases`,'POST',{question:$('cq').value,expected:$('ce').value});tsd(id);tsl()};
 $('rgo').onclick=async()=>{const r=await api(`api/testsets/${id}/run`,'POST',{agent_id:+$('ra').value,agent_version:$('rv').value?+$('rv').value:null});flash(`Đang chạy lần #${r.test_run_id} (v${r.agent_version})…`);tsd(id)};
 clearTimeout(poll);if(d.test_runs.some(t=>t.status==='đang chạy'))poll=setTimeout(()=>tsd(id),4000)}
async function cdel(c,s){await api('api/testcases/'+c,'DELETE');tsd(s)}
$('tsadd').onclick=async()=>{const r=await api('api/testsets','POST',{name:$('tsn').value});tsl();tsd(r.id)};
$('go').onclick=runs;$('sa').onchange=stats;
(async()=>{agents=await api('api/agents');const o=agents.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('');$('fa').insertAdjacentHTML('beforeend',o);$('sa').insertAdjacentHTML('beforeend',o);stats();runs();tsl()})();""")

# ---------------- SETTINGS ----------------
SETTINGS = page("settings", "Cài đặt", """
<main><div class="grid2"><div class="card"><h2>Gemini API key</h2><div id="key"></div><p><small>Key nằm trong /opt/langchain/.env (quyền 600). Giao diện không bao giờ hiển thị key.</small></p>
<h2 style="margin-top:14px">Bảng giá</h2><div id="price"></div></div>
<div class="card"><div class="row"><h2 style="margin:0">Hệ thống</h2><button class="s" id="rl">Làm mới</button></div><div id="sys"></div></div></div>
<div class="card"><h2>Xuất / nhập agent (JSON)</h2><div class="row">Xuất: <select id="ex"></select><button id="exb" class="g">Tải JSON</button></div>
<label>Nhập: dán JSON hoặc chọn file</label><textarea id="im" rows="6" placeholder='{"format":"moza-agent/1","config":{...}}'></textarea>
<div class="row"><input type="file" id="imf" accept=".json"><button id="imb">Nhập thành agent mới</button></div></div></main>""", r"""
async function load(){const s=await api('api/settings');
 $('key').innerHTML=`<p>Trạng thái: <b>${esc(s.gemini_key.masked)}</b><br>Kiểm tra hoạt động: ${s.gemini_key.works===true?'<span class="ok">OK</span>':'<span class="err">'+esc(s.gemini_key.works)+'</span>'}<br>Model mặc định: ${esc(s.default_model)}</p>`;
 $('price').innerHTML='<table><tr><th>Model</th><th>Vào /1M</th><th>Ra /1M</th><th>Ghi chú</th></tr>'+Object.entries(s.pricing).map(([m,p])=>`<tr><td>${esc(m)}</td><td>$${p.input_per_1m}</td><td>$${p.output_per_1m}</td><td>${esc(p.note)}</td></tr>`).join('')+`</table><small>${esc(s.pricing_source.note)} Nguồn: <a href="${esc(s.pricing_source.url)}" target="_blank" rel="noopener">${esc(s.pricing_source.url)}</a> · kiểm ngày ${esc(s.pricing_source.checked)}</small>`;
 const r=s.ram,d=s.disk;$('sys').innerHTML=`<div class="kpi"><div>RAM trống<b>${r.available_mb} / ${r.total_mb} MB</b></div><div>deepagent-api<b>${r.api_cgroup_mb??r.api_rss_mb} MB</b>giới hạn ${esc(r.api_limit)}</div><div>Đĩa<b>${d.pct}%</b>${d.used_gb}/${d.total_gb} GB</div></div>
 <h4>Dịch vụ</h4>${Object.entries(s.services).map(([k,v])=>`<span class="tag ${v==='active'?'ro':'bl'}">${esc(k)}: ${esc(v)}</span>`).join(' ')}
 <h4>Container</h4>${s.containers.map(c=>`<span class="tag">${esc(c)}</span>`).join(' ')}<h4>Agent đang nạp trong RAM</h4>${s.cached_agents.map(c=>`<span class="tag">${esc(c)}</span>`).join(' ')||'–'}`;
 const a=await api('api/agents');$('ex').innerHTML=a.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}
$('rl').onclick=load;$('exb').onclick=()=>{location.href='api/agents/'+$('ex').value+'/export'};
$('imf').onchange=async()=>{$('im').value=await $('imf').files[0].text()};
$('imb').onclick=async()=>{try{const r=await api('api/agents/import','POST',JSON.parse($('im').value));flash('Đã nhập → agent #'+r.id);load()}catch(e){flash(e.message,1)}};
load();""")


# ---------------- TRACES ----------------
TRACES = page("traces", "Traces", """
<style>.tree{font-size:13px}.spn{display:grid;grid-template-columns:minmax(260px,38%) 1fr 160px;gap:8px;align-items:center;border-bottom:1px solid #f1f1f1;padding:3px 0}
.bar{height:14px;border-radius:3px;background:#93c5fd;position:relative}.k-llm .bar{background:#60a5fa}.k-tool .bar{background:#34d399}.k-subagent .bar{background:#c084fc}.k-todos .bar{background:#fbbf24}
.k-error .bar{background:#f87171}.lane{position:relative;height:14px;background:#f8fafc}</style>
<main><div class="card"><div class="row"><h2 style="margin:0">Traces</h2><select id="fa"><option value="">mọi agent</option></select><input id="fq" placeholder="tìm"><button id="go">Lọc</button></div>
<table id="runs"></table></div><div class="card" id="tr" style="display:none"></div></main>""", r"""
async function list(){const p=new URLSearchParams();if($('fa').value)p.set('agent_id',$('fa').value);if($('fq').value)p.set('q_',$('fq').value);p.set('limit',60);
 const rs=await api('api/runs?'+p);$('runs').innerHTML='<tr><th>#</th><th>Thời gian</th><th>Agent</th><th>Câu hỏi</th><th>Tool</th><th>Token</th><th>Chi phí</th><th>Trễ</th><th>Trạng thái</th></tr>'+
 rs.map(r=>`<tr class="r" onclick="show(${r.id})"><td>${r.id}</td><td>${vn(r.ts)}</td><td>${esc(r.agent_name||'–')} v${r.agent_version??''}</td><td>${esc(r.user_input)}</td><td>${r.n_tools}</td><td>${r.total_tokens}</td><td>${usd(r.cost_usd)}</td><td>${(r.latency_ms/1000).toFixed(1)}s</td><td class="${r.error?'err':'ok'}">${r.error?'lỗi':'ok'}</td></tr>`).join('')}
async function show(id){const d=await api('api/runs/'+id),r=d.run,sp=r.spans||[],tot=Math.max(r.latency_ms,1);
 const kids={};sp.forEach(s=>{(kids[s.parent||'root']=kids[s.parent||'root']||[]).push(s)});
 const icon={llm:'<svg class="ic"><use href="#i-sparkles"/></svg>',tool:'<svg class="ic"><use href="#i-wrench"/></svg>',subagent:'<svg class="ic"><use href="#i-bot"/></svg>',todos:'<svg class="ic"><use href="#i-list"/></svg>'};
 function row(s,dep){const st=s.status==='error'?' k-error':'';const left=(s.start_ms/tot*100).toFixed(2),w=Math.max(((s.ms||0)/tot*100),0.4).toFixed(2);
  const meta=s.kind==='llm'?`${s.input_tokens??0}→${s.output_tokens??0} tok · ${usd(s.cost_usd)}`:'';
  let h=`<div class="spn k-${s.kind}${st}"><div style="padding-left:${dep*18}px" title="${esc(JSON.stringify(s.args||s.error||''))}">${icon[s.kind]||'•'} <b>${esc(s.name)}</b>${s.attempt>1?' <span class="tag">thử lại '+s.attempt+'</span>':''}${s.status==='error'?' <span class="tag bl">lỗi</span>':''}</div>
  <div class="lane"><div class="bar" style="position:absolute;left:${left}%;width:${w}%"></div></div><div><small>${s.ms??'?'} ms ${meta}</small></div></div>`;
  (kids[s.id]||[]).forEach(c=>h+=row(c,dep+1));return h}
 const box=$('tr');box.style.display='block';
 box.innerHTML=`<h3>Run #${r.id} · ${esc(r.thread_id)} · agent #${r.agent_id??'–'} v${r.agent_version??'–'} ${r.deployment?'· deployment '+esc(r.deployment):''}</h3>
 <div class="kpi"><div>Tổng thời gian<b>${(r.latency_ms/1000).toFixed(2)}s</b></div><div>Token<b>${r.total_tokens}</b>${r.input_tokens} vào / ${r.output_tokens} ra</div><div>Chi phí<b>${usd(r.cost_usd)}</b></div><div>Model<b style="font-size:13px">${esc((r.models_used||[r.model]).join(' → '))}</b></div><div>Span<b>${sp.length}</b></div></div>
 <p><b>Hỏi:</b> ${esc(r.user_input)}<br><b>Đáp:</b> ${esc(r.answer)}</p>${r.error?`<p class="err">${esc(r.error)}</p>`:''}
 <h4>Span tree (LLM <svg class="ic"><use href="#i-sparkles"/></svg> · tool <svg class="ic"><use href="#i-wrench"/></svg> · sub-agent <svg class="ic"><use href="#i-bot"/></svg> · plan <svg class="ic"><use href="#i-list"/></svg>)</h4><div class="tree"><div class="spn" style="font-weight:bold"><div><svg class="ic"><use href="#i-flag"/></svg> run #${r.id}</div><div class="lane"><div class="bar" style="width:100%;background:#cbd5e1"></div></div><div><small>${r.latency_ms} ms</small></div></div>${(kids.root||[]).map(s=>row(s,1)).join('')||'<p>(run cũ – chưa có span)</p>'}</div>
 <details><summary>JSON span</summary><pre>${esc(JSON.stringify(sp,null,1))}</pre></details>`;box.scrollIntoView({behavior:'smooth'})}
$('go').onclick=list;
(async()=>{const a=await api('api/agents');$('fa').insertAdjacentHTML('beforeend',a.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join(''));await list();const r=new URLSearchParams(location.search).get('run');if(r)show(+r)})();""")

# ---------------- PLAYGROUND ----------------
PLAYGROUND = page("playground", "Playground", """
<main><div class="grid2"><div class="card"><h2>Playground</h2><small>Biến dạng {{ten_bien}}. Gọi qua Gateway (có fallback).</small>
<label>Prompt</label><textarea id="tp" rows="10">Bạn là trợ lý nhà Moza. Hãy viết một câu chào {{ten}} vào buổi {{buoi}}, giọng {{giong}}.</textarea>
<label>Biến (JSON)</label><textarea id="tv" rows="3">{"ten":"anh Huy","buoi":"sáng","giong":"thân thiện"}</textarea>
<div class="row"><select id="tm"></select><label>Temperature <input id="tt" type="number" min="0" max="2" step="0.1" value="0.7" style="width:70px"></label>
<label><input type="checkbox" id="tf" checked> fallback</label><button id="run">Chạy</button></div>
<pre id="out"></pre><small id="meta"></small>
<h3 style="margin-top:12px">Lưu vào thư viện prompt</h3><div class="row"><input id="pn" placeholder="tên prompt"><input id="pnote" placeholder="ghi chú phiên bản"><button id="save" class="g">Lưu phiên bản</button></div></div>
<div class="card"><h2>Thư viện prompt</h2><table id="pl"></table><div id="pv"></div></div></div></main>""", r"""
let last=null;
async function lib(){const p=await api('api/prompts');$('pl').innerHTML='<tr><th>Tên</th><th>Mới nhất</th><th>Cập nhật</th><th></th></tr>'+p.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td>v${x.latest}</td><td>${vn(x.updated)}</td><td><button class="s" onclick="vers(${x.id},'${esc(x.name).replace(/'/g,'')}')">Phiên bản</button> <button class="s" onclick="delP(${x.id})">Xoá</button></td></tr>`).join('')}
async function vers(id,n){const v=await api('api/prompts/'+id);window._pv=v;$('pv').innerHTML=`<h3>${esc(n)}</h3>`+v.map((x,i)=>`<div class="card" style="margin-top:6px"><b>v${x.version}</b> · ${esc(x.model)} · t=${x.temperature} · ${vn(x.created)} ${x.note?'· '+esc(x.note):''} <button class="s" onclick="load_(${i},'${esc(n).replace(/'/g,'')}')">Mở trong playground</button><pre>${esc(x.template)}</pre></div>`).join('')}
function load_(i,n){const x=window._pv[i];$('tp').value=x.template;$('tv').value=JSON.stringify(x.variables||{});$('tm').value=x.model;$('tt').value=x.temperature;$('pn').value=n;window.scrollTo(0,0)}
async function delP(id){if(confirm('Xoá prompt?')){await api('api/prompts/'+id,'DELETE');lib();$('pv').innerHTML=''}}
$('run').onclick=async()=>{$('out').textContent='…';try{const r=await api('api/playground/run','POST',{template:$('tp').value,variables:JSON.parse($('tv').value||'{}'),model:$('tm').value,temperature:+$('tt').value,fallback:$('tf').checked});
 last=r;$('out').textContent=r.output;$('meta').textContent=`${r.model} · ${r.input_tokens}→${r.output_tokens} token · ${usd(r.cost_usd)} · ${r.ms} ms`}catch(e){$('out').textContent='Lỗi: '+e.message}};
$('save').onclick=async()=>{try{const r=await api('api/prompts','POST',{name:$('pn').value,template:$('tp').value,model:$('tm').value,temperature:+$('tt').value,variables:JSON.parse($('tv').value||'{}'),note:$('pnote').value||null});flash('Đã lưu v'+r.version);lib();vers(r.id,$('pn').value)}catch(e){flash(e.message,1)}};
(async()=>{const m=await api('api/models');$('tm').innerHTML=m.models.map(x=>`<option ${x===m.default?'selected':''}>${esc(x)}</option>`).join('');lib()})();""")

# ---------------- DEPLOYMENTS ----------------
DEPLOYMENTS = page("deployments", "Deployments", """
<main><div class="card"><h2>Deploy agent</h2><div class="row"><select id="da"></select><input id="dv" type="number" min="1" placeholder="phiên bản (trống = hiện tại)" style="width:200px">
<input id="ds" placeholder="đường dẫn, vd tro-ly-nha"><button id="dgo">Deploy</button></div><div id="dkey"></div>
<p style="font-size:13px">Mỗi deployment có endpoint riêng <code>POST /d/&lt;slug&gt;/invoke</code> với header <code>X-API-Key</code> (không cần basic auth). Server chỉ lưu SHA-256 của key.</p></div>
<div class="card"><h2>Deployments</h2><table id="dl"></table></div></main>""", r"""
let agents=[];
async function list(){const d=await api('api/deployments');$('dl').innerHTML='<tr><th>Slug</th><th>Agent</th><th>Phiên bản</th><th>Trạng thái</th><th>Key</th><th>Run / lỗi</th><th>Lịch sử</th><th></th></tr>'+d.map(x=>`<tr><td><b>${esc(x.slug)}</b><br><small>/d/${esc(x.slug)}/invoke</small></td><td>${esc(x.agent_name)}</td>
 <td>v${x.version}${x.current_version!==x.version?` <small>(mới nhất v${x.current_version})</small>`:''}<br><select onchange="setv(${x.id},this.value)"><option value="">đổi phiên bản…</option>${Array.from({length:x.current_version},(_,i)=>i+1).reverse().map(v=>`<option value="${v}">v${v}</option>`).join('')}</select></td>
 <td><span class="tag ${x.status==='active'?'ro':'bl'}">${x.status}</span></td><td>${esc(x.key_prefix)}…</td><td>${x.runs} / ${x.errors}</td>
 <td><small>${(x.history||[]).slice(0,5).map(h=>`${vn(h.ts)} ${esc(h.action)} v${h.version}`).join('<br>')}</small></td>
 <td class="row"><button class="s" onclick="st(${x.id},'${x.status==='active'?'paused':'active'}')">${x.status==='active'?'Tạm dừng':'Bật lại'}</button><button class="s" onclick="rot(${x.id})">Đổi key</button><button class="s" onclick="del_(${x.id})">Xoá</button></td></tr>`).join('')}
function showKey(k,slug){$('dkey').innerHTML=`<div class="card" style="background:#fefce8"><b>API key (chỉ hiện một lần):</b><pre>${esc(k)}</pre><small>Thử: curl -X POST ${esc(location.origin)}/d/${esc(slug)}/invoke -H "X-API-Key: …" -H "Content-Type: application/json" -d '{"message":"xin chào"}'</small></div>`}
$('dgo').onclick=async()=>{try{const r=await api('api/deployments','POST',{agent_id:+$('da').value,version:$('dv').value?+$('dv').value:null,slug:$('ds').value});showKey(r.api_key,r.slug);list()}catch(e){flash(e.message,1)}};
async function setv(id,v){if(!v)return;await api('api/deployments/'+id,'PATCH',{version:+v});flash('Đã chuyển sang v'+v);list()}
async function st(id,s){await api('api/deployments/'+id,'PATCH',{status:s});list()}
async function rot(id){if(!confirm('Đổi API key? Key cũ hết hiệu lực ngay.'))return;const r=await api('api/deployments/'+id,'PATCH',{rotate_key:true});showKey(r.api_key,'…');list()}
async function del_(id){if(confirm('Xoá deployment?')){await api('api/deployments/'+id,'DELETE');list()}}
(async()=>{agents=await api('api/agents');$('da').innerHTML=agents.map(a=>`<option value="${a.id}">${esc(a.name)} (v${a.current_version})</option>`).join('');list()})();""")

# ---------------- FLEET ----------------
FLEET = page("fleet", "Fleet", """
<style>.fl{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.fc h3{margin:0 0 4px}</style>
<main><div class="card"><div class="row"><h2 style="margin:0">Fleet – toàn bộ agent</h2><span style="flex:1"></span><a href="agents"><button>+ Tạo agent</button></a></div><div class="kpi" id="tot" style="margin-top:8px"></div></div>
<div class="fl" id="fl"></div></main>""", r"""
(async()=>{const f=await api('api/fleet');const sum=k=>f.reduce((a,x)=>a+Number(x[k]||0),0);
 $('tot').innerHTML=[['Agent',f.length],['Run',sum('runs')],['Run 24h',sum('runs_24h')],['Lỗi',sum('errors')],['Token',sum('tokens')],['Chi phí',usd(sum('cost'))],['Deployment',f.reduce((a,x)=>a+(x.deployments||[]).length,0)]].map(([k,v])=>`<div>${k}<b>${esc(v)}</b></div>`).join('');
 $('fl').innerHTML=f.map(a=>`<div class="card fc"><h3>${esc(a.name)} <small>v${a.current_version}</small></h3><small>${esc(a.description)}</small>
 <div style="margin:6px 0"><span class="tag">${esc(a.model)}</span><span class="tag">${a.n_tools} tool</span><span class="tag">${a.n_sub} sub-agent</span>${a.sandbox?'<span class="tag bl">sandbox</span>':''}</div>
 <table><tr><td>Run (24h)</td><td>${a.runs} (${a.runs_24h})</td></tr><tr><td>Lỗi</td><td class="${a.errors?'err':''}">${a.errors}${a.runs?' ('+(a.errors/a.runs*100).toFixed(0)+'%)':''}</td></tr>
 <tr><td>Token / chi phí</td><td>${a.tokens} / ${usd(a.cost)}</td></tr><tr><td>Điểm TB</td><td>${a.avg_score??'–'}</td></tr><tr><td>Run gần nhất</td><td>${vn(a.last_run)||'–'}</td></tr>
 <tr><td>Deploy</td><td>${(a.deployments||[]).map(d=>`<span class="tag ${d.status==='active'?'ro':'bl'}">${esc(d.slug)} v${d.version}</span>`).join('')||'–'}</td></tr></table>
 <div class="row" style="margin-top:6px"><a href="./?agent=${a.id}"><button class="s">Chat</button></a><a href="agents?edit=${a.id}"><button class="s">Sửa</button></a><a href="traces"><button class="s">Traces</button></a><a href="deployments"><button class="s">Deploy</button></a></div></div>`).join('')})();""")

# ---------------- GATEWAY ----------------
GATEWAY = page("gateway", "Gateway", """
<main><div class="grid2"><div class="card"><h2>LLM Gateway</h2><div id="info"></div>
<h3 style="margin-top:12px">Quản lý key</h3><p style="font-size:13px">Key không bao giờ hiển thị; chỉ hiện dấu vân tay SHA-256. Thay key: key mới được kiểm tra trước khi ghi vào /opt/langchain/.env (600).</p>
<div class="row"><input id="nk" type="password" placeholder="Gemini API key mới" style="flex:1" autocomplete="off"><button id="setk" class="r">Thay key</button></div></div>
<div class="card"><h2>Theo model (7 ngày)</h2><table id="bm"></table><h2 style="margin-top:12px">Theo agent (7 ngày)</h2><table id="ba"></table></div></div>
<div class="card"><h2>Lời gọi gần đây</h2><table id="rc"></table></div></main>""", r"""
async function load(){const g=await api('api/gateway');
 $('info').innerHTML=`<p>Nhà cung cấp: <b>${esc(g.key.provider)}</b> · key: <b>${g.key.configured?'đã cấu hình':'chưa có'}</b> <span class="tag">${esc(g.key.fingerprint||'')}</span></p>
 <p>Chuỗi mặc định (retry 2 lần/model, rồi chuyển model khi 429/503): <b>${g.default_chain.map(esc).join(' → ')}</b></p><p><small>Mỗi agent có model chính, model dự phòng, giới hạn RPM và ngân sách/ngày riêng (trang Agents).</small></p>
 <table><tr><th>Model</th><th>Vào /1M</th><th>Ra /1M</th><th></th></tr>${Object.entries(g.pricing).map(([m,p])=>`<tr><td>${esc(m)}</td><td>$${p.input_per_1m}</td><td>$${p.output_per_1m}</td><td><small>${esc(p.note)}</small></td></tr>`).join('')}</table><small>Nguồn ${esc(g.pricing_source.url)} · kiểm ${esc(g.pricing_source.checked)}</small>`;
 $('bm').innerHTML='<tr><th>Model</th><th>Gọi</th><th>Lỗi</th><th>Token vào/ra</th><th>Chi phí</th><th>Trễ TB</th></tr>'+g.by_model.map(x=>`<tr><td>${esc(x.model)}</td><td>${x.calls}</td><td class="${x.errors?'err':''}">${x.errors}</td><td>${x.in_tok}/${x.out_tok}</td><td>${usd(x.cost)}</td><td>${x.avg_ms} ms</td></tr>`).join('');
 $('ba').innerHTML='<tr><th>Agent</th><th>Gọi</th><th>Lỗi</th><th>Hôm nay / ngân sách</th><th>RPM</th><th>Chuỗi model</th></tr>'+g.by_agent.map(x=>`<tr><td>${esc(x.name||'(giám khảo/playground)')}</td><td>${x.calls}</td><td>${x.errors}</td><td>${usd(x.cost_today)} / ${x.budget?'$'+x.budget:'∞'}</td><td>${x.rpm??'–'}</td><td><small>${esc([x.model,...(x.fallbacks||[])].filter(Boolean).join(' → '))}</small></td></tr>`).join('');
 $('rc').innerHTML='<tr><th>Thời gian</th><th>Agent</th><th>Model</th><th>Trạng thái</th><th>Token</th><th>Chi phí</th><th>Trễ</th><th>Lỗi</th></tr>'+g.recent.map(x=>`<tr><td>${vn(x.ts)}</td><td>${x.agent_id??'–'}</td><td>${esc(x.model)}</td><td class="${x.status==='ok'?'ok':'err'}">${esc(x.status)}</td><td>${x.input_tokens}/${x.output_tokens}</td><td>${usd(x.cost_usd)}</td><td>${x.latency_ms} ms</td><td><small>${esc(x.error||'')}</small></td></tr>`).join('')}
$('setk').onclick=async()=>{if(!confirm('Thay Gemini API key cho toàn hệ thống?'))return;try{const r=await api('api/gateway/key','POST',{api_key:$('nk').value});$('nk').value='';flash('Đã thay key: '+r.fingerprint);load()}catch(e){flash(e.message,1)}};
load();""")

PAGES = {"/": CHAT, "/agents": AGENTS, "/tools": TOOLS, "/memory": MEMORY, "/eval": EVAL, "/settings": SETTINGS,
         "/traces": TRACES, "/playground": PLAYGROUND, "/deployments": DEPLOYMENTS, "/fleet": FLEET, "/gateway": GATEWAY}
