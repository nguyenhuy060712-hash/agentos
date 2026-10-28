"""UI cho P1 (Projects/Traces/Threads/Dashboard/Alerts/Rules/Share) và P2 (Datasets/Experiments/Evaluators/Annotation)."""
import json, html
from ui import page, CSS

CHART_JS = r"""
function chart(el,pts,key,opt={}){const W=el.clientWidth||520,H=opt.h||150,P=30;const vs=pts.map(p=>Number(p[key]||0));const mx=Math.max(...vs,opt.min||0.0001);
 const x=i=>P+(pts.length<2?0:i*(W-P-8)/(pts.length-1)),y=v=>H-18-(v/mx)*(H-30);
 const path=vs.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1)).join(' ');
 const lbl=pts.length?[0,Math.floor(pts.length/2),pts.length-1].map(i=>`<text x="${x(i)}" y="${H-3}" font-size="9" text-anchor="middle" fill="#666">${esc(String(pts[i].t).slice(5,16))}</text>`).join(''):'';
 el.innerHTML=`<div style="font-size:12px;color:#374151">${esc(opt.title||key)} <b>${opt.fmt?opt.fmt(vs[vs.length-1]??0):(vs[vs.length-1]??0)}</b> <small>(max ${opt.fmt?opt.fmt(mx):mx.toFixed(2)})</small></div>
 <svg width="${W}" height="${H}"><line x1="${P}" y1="${H-18}" x2="${W}" y2="${H-18}" stroke="#ddd"/><text x="2" y="12" font-size="9" fill="#666">${opt.fmt?opt.fmt(mx):mx.toFixed(1)}</text>
 ${opt.bar?vs.map((v,i)=>`<rect x="${x(i)-3}" y="${y(v)}" width="6" height="${H-18-y(v)}" fill="${opt.color||'#60a5fa'}"/>`).join(''):`<path d="${path}" fill="none" stroke="${opt.color||'#2563eb'}" stroke-width="2"/>`}
 ${vs.map((v,i)=>`<circle cx="${x(i)}" cy="${y(v)}" r="2" fill="${opt.color||'#2563eb'}"><title>${esc(String(pts[i].t))}: ${v}</title></circle>`).join('')}${lbl}</svg>`}
function treeHtml(runs,fbs){const kids={};runs.forEach(r=>{(kids[r.parent_id||'root']=kids[r.parent_id||'root']||[]).push(r)});const root=(kids.root||[])[0];if(!root)return '';
 const t0=new Date(root.start_time).getTime(),tot=Math.max(root.latency_ms||1,1);const ic={llm:'<svg class="ic"><use href="#i-sparkles"/></svg>',tool:'<svg class="ic"><use href="#i-wrench"/></svg>',chain:'<svg class="ic"><use href="#i-waypoints"/></svg>',retriever:'<svg class="ic"><use href="#i-library"/></svg>',prompt:'<svg class="ic"><use href="#i-file-text"/></svg>',parser:'<svg class="ic"><use href="#i-box"/></svg>'};
 function row(r,d){const l=((new Date(r.start_time).getTime()-t0)/tot*100),w=Math.max((r.latency_ms||0)/tot*100,.4);const f=(fbs||[]).filter(x=>x.run_id===r.id);
  let h=`<div class="spn" onclick="sel('${r.id}')" style="cursor:pointer"><div style="padding-left:${d*16}px">${ic[r.run_type]||'•'} <b>${esc(r.name)}</b> ${r.status==='error'?'<span class="tag bl">lỗi</span>':''}${f.map(x=>`<span class="tag ro">${esc(x.key)}:${x.score??esc(x.value)}</span>`).join('')}</div>
  <div class="lane"><div class="bar ${r.run_type}" style="left:${Math.min(l,99.5)}%;width:${Math.min(w,100)}%"></div></div><div><small>${r.latency_ms??'…'} ms${r.total_tokens?' · '+r.total_tokens+' tok':''}${r.cost_usd?' · '+usd(r.cost_usd):''}</small></div></div>`;
  (kids[r.id]||[]).forEach(c=>h+=row(c,d+1));return h}
 return row(root,0)}
"""
TREE_CSS = """.spn{display:grid;grid-template-columns:minmax(260px,40%) 1fr 190px;gap:8px;align-items:center;border-bottom:1px solid #f3f4f6;padding:3px 0;font-size:13px}
.spn:hover{background:#f8fafc}.lane{position:relative;height:12px;background:#f8fafc}.bar{position:absolute;height:12px;border-radius:3px;background:#a5b4fc}
.bar.llm{background:#60a5fa}.bar.tool{background:#34d399}.bar.chain{background:#c4b5fd}.tabs a{margin-right:12px;cursor:pointer;color:#2563eb}.tabs a.on{font-weight:bold;color:#111;border-bottom:2px solid #2563eb}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}code{background:#f3f4f6;padding:1px 4px;border-radius:4px}"""

PROJECTS = page("projects", "Projects", f"""<style>{TREE_CSS}.env{{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:10px;font-size:11.5px;font-weight:600;text-transform:uppercase;border:1px solid transparent;background:#eef2f6;color:#475467}}.env-prod{{background:#fdecea;color:#b42318;border-color:#f7c6c1}}.env-staging{{background:#fef4e0;color:#93370d;border-color:#f6dca8}}.env-dev{{background:#e6f4ea;color:#067647;border-color:#b7e2c4}}</style>
<main><div class="card"><div class="row"><h2 style="margin:0">Projects</h2><span style="flex:1"></span><input id="np" placeholder="tên project mới"><select id="npe" title="Môi trường"><option value="prod">prod</option><option value="staging">staging</option><option value="dev">dev</option></select><button id="npb">Tạo</button></div><table id="pl"></table></div>
<div class="card" id="pv" style="display:none"><div class="row"><h2 style="margin:0" id="pt"></h2><span id="penv"></span><span style="flex:1"></span>
<span class="tabs"><a data-t="runs" class="on">Runs</a><a data-t="threads">Threads</a><a data-t="errors">Lỗi (gom cụm)</a><a data-t="dash">Dashboard</a><a data-t="setup">Cài đặt & SDK</a></span></div>
<div id="tab_runs"><div class="row" style="margin-top:8px"><input id="flt" style="flex:1;font-family:monospace" placeholder='and(eq(status,"error"), gt(latency,2), has(tags,"agent:1"), search("nhiệt độ"), gte(feedback.dung,4), eq(metadata.agent_version,"2"))'>
<label><input type="checkbox" id="root" checked> chỉ trace gốc</label><select id="subag" title="Chỉ các run con do một sub-agent tạo"><option value="">sub-agent: tất cả</option></select><select id="since"><option value="">mọi lúc</option><option value="1">1 giờ</option><option value="24" selected>24 giờ</option><option value="168">7 ngày</option></select><button id="go">Lọc</button></div>
<div class="row" style="margin-top:6px"><small>Mẫu:</small>
<button class="s" data-f='eq(status,"error")'>lỗi</button><button class="s" data-f='gt(latency,5)'>chậm &gt;5s</button><button class="s" data-f='eq(run_type,"llm")'>LLM</button>
<button class="s" data-f='lt(feedback.user_score,3)'>điểm thấp</button><button class="s" data-f='has(tags,"deployment")'>từ deployment</button>
<span style="margin-left:10px"><small>Đã lưu:</small> <span id="sfl"></span><button class="s" id="sfb"><svg class="ic"><use href="#i-bookmark"/></svg> Lưu bộ lọc</button></span></div>
<div class="row" style="margin-top:6px"><small>Với các dòng đã chọn:</small><select id="bulkds"></select><button class="s" id="tods">→ Dataset</button><select id="bulkq"></select><button class="s" id="toq">→ Annotation queue</button></div>
<table id="rl" style="margin-top:6px"></table></div>
<div id="tab_threads" style="display:none"><div class="row"><small>Chấm cả thread (multi-turn) bằng evaluator:</small><select id="tev"></select></div><table id="tl"></table><div id="td"></div></div>
<div id="tab_errors" style="display:none"><div class="row" style="margin:8px 0"><select id="eh"><option value="24">24 giờ</option><option value="168" selected>7 ngày</option><option value="720">30 ngày</option></select><select id="em"><option value="signature">Gom theo chữ ký</option><option value="semantic">Gom theo ngữ nghĩa (embedding)</option></select><button class="s" onclick="errors()">Cập nhật</button><span id="etot"></span></div><table id="el"></table></div>
<div id="tab_dash" style="display:none"><div class="row" style="margin:8px 0"><select id="dh"><option value="1">1 giờ</option><option value="24" selected>24 giờ</option><option value="168">7 ngày</option><option value="720">30 ngày</option></select>
<select id="db"><option value="minute">phút</option><option value="hour" selected>giờ</option><option value="day">ngày</option></select><button id="dgo" class="s">Cập nhật</button></div>
<div class="kpi" id="dk"></div><div class="charts" style="margin-top:10px"><div class="card" id="c1"></div><div class="card" id="c2"></div><div class="card" id="c3"></div><div class="card" id="c4"></div><div class="card" id="c5"></div><div class="card" id="c6"></div></div>
<div class="grid2" style="margin-top:10px"><div class="card"><h3>Theo model (LLM)</h3><table id="dl"></table></div><div class="card"><h3>Tool</h3><table id="dt"></table><h3>Feedback</h3><table id="df"></table></div></div></div>
<div id="tab_setup" style="display:none"><div class="grid2"><div><h3>Retention</h3><div class="row"><input id="ret" type="number" min="1" style="width:100px"> ngày <button id="retb" class="s">Lưu</button></div>
<h3>Môi trường của project</h3><div class="row"><select id="pe"><option value="prod">prod</option><option value="staging">staging</option><option value="dev">dev</option></select><button id="peb" class="s">Lưu</button></div><small>Run gửi lên mà không ghi môi trường sẽ mang nhãn này.</small>
<h3>Ingest API key</h3><div class="row"><input id="kl" placeholder="nhãn"><select id="ke" title="Môi trường của key"><option value="">theo dự án</option><option value="prod">prod</option><option value="staging">staging</option><option value="dev">dev</option></select><button id="kb" class="s">Tạo key</button></div><div id="knew"></div><table id="kt"></table></div>
<div><h3>Trace code Python bất kỳ (SDK)</h3><pre>pip-free: copy sdk/agentos_sdk.py (repo /opt/agentos)
import agentos_sdk as ao
ao.configure(endpoint="https://&lt;url&gt;", api_key="ak_...")
# trên #83 dùng endpoint="http://127.0.0.1:8020"

@ao.traceable(run_type="retriever")
def tim(q): return ["doc1", "doc2"]

@ao.traceable(tags=["demo"])
def tra_loi(q):
    docs = tim(q)
    with ao.trace("goi-llm", run_type="llm") as r:
        r.set_usage(120, 30, model="gemini-3.6-flash")
        r.end(outputs={{"text": "..."}})
    return "xong"

tra_loi("câu hỏi", agentos_thread_id="phien-1")
ao.flush()</pre></div></div></div></div>
<div class="card" id="trv" style="display:none"></div></main>""", CHART_JS + r"""
let P=null,TAB='runs';
async function plist(){const ps=await api('api/obs/projects');$('pl').innerHTML='<tr><th>#</th><th>Dự án</th><th>Môi trường</th><th>Mô tả</th><th>Trace</th><th>24h</th><th>Lỗi 24h</th><th>Retention</th><th>Gần nhất</th></tr>'+
 ps.map(p=>`<tr class="r" onclick="openP(${p.id},'${esc(p.name).replace(/'/g,'')}',${p.retention_days},'${esc(p.environment||'prod')}')"><td>${p.id}</td><td><b>${esc(p.name)}</b></td><td>${envB(p.environment)}</td><td>${esc(p.description)}</td><td>${p.traces}</td><td>${p.traces_24h}</td><td class="${p.errors_24h?'err':''}">${p.errors_24h}</td><td>${p.retention_days} ngày</td><td>${vn(p.last_run)}</td></tr>`).join('')}
const envB=e=>'<span class="env env-'+esc(e||'prod')+'" title="Môi trường">'+esc(e||'prod')+'</span>';
function openP(id,n,ret,env){P=id;env=env||'prod';$('penv').innerHTML=envB(env);const o=$('pe');if(![...o.options].some(x=>x.value===env))o.add(new Option(env,env));o.value=env;$('pv').style.display='block';$('pt').textContent='Dự án: '+n;$('ret').value=ret;history.replaceState(null,'','?p='+id);subs();tab(TAB)}
async function subs(){const o=$('subag');o.length=1;try{(await api('api/obs/subagents?project_id='+P)).forEach(n=>o.add(new Option(n,n)))}catch(e){}}
function tab(t){TAB=t;document.querySelectorAll('.tabs a').forEach(a=>a.classList.toggle('on',a.dataset.t===t));['runs','threads','errors','dash','setup'].forEach(x=>$('tab_'+x).style.display=x===t?'block':'none');
 ({runs:runs,threads:threads,errors:errors,dash:dash,setup:setup})[t]()}
document.querySelectorAll('.tabs a').forEach(a=>a.onclick=()=>tab(a.dataset.t));
document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{$('flt').value=b.dataset.f;runs()});
async function sfl(){const f=await api('api/obs/saved_filters?project_id='+P);$('sfl').innerHTML=f.map(x=>`<span class="tag" style="cursor:pointer" title="${esc(x.filter)}" onclick="$('flt').value=this.title;runs()">${esc(x.name)} <a onclick="event.stopPropagation();api('api/obs/saved_filters/${x.id}','DELETE').then(sfl)">×</a></span>`).join('')}
$('sfb').onclick=async()=>{if(!$('flt').value)return flash('Nhập bộ lọc trước',1);const n=prompt('Tên bộ lọc:');if(!n)return;try{await api('api/obs/saved_filters','POST',{project_id:P,name:n,filter:$('flt').value});sfl()}catch(e){flash(e.message,1)}};
async function errors(){const d=await api(`api/obs/projects/${P}/errors?hours=${$('eh').value}&mode=${$('em').value}`);$('etot').textContent=` ${d.total_errors} run lỗi · ${d.clusters.length} cụm`;
 $('el').innerHTML='<tr><th>Số lần</th><th>Trace</th><th>Chữ ký lỗi (đã chuẩn hoá)</th><th>Run</th><th>Lần đầu / gần nhất</th><th>Ví dụ</th></tr>'+d.clusters.map(c=>`<tr><td><b>${c.count}</b></td><td>${c.n_traces}</td><td><code>${esc(c.signature)}</code>${(c.members||[]).length>1?'<br><small>gộp: '+c.members.slice(1).map(m=>esc(m.signature.slice(0,60))+' ('+m.count+', sim '+m.similarity+')').join('<br>')+'</small>':''}</td><td>${c.names.map(([n,k])=>`<span class="tag">${esc(n)}×${k}</span>`).join('')}</td><td>${vn(c.first)}<br>${vn(c.last)}</td><td><a href="#" onclick="openT('${c.sample_trace}');return false">trace</a><br><small>${esc(c.sample_error.slice(0,140))}</small></td></tr>`).join('')||'<tr><td>Không có lỗi</td></tr>'}
async function runs(){if(TAB==='runs')sfl();const p=new URLSearchParams({project_id:P,filter:$('flt').value,root_only:$('root').checked,limit:150});if($('subag').value){p.set('lc_agent_name',$('subag').value);p.set('root_only',false)}if($('since').value)p.set('since_hours',$('since').value);
 try{const rs=await api('api/obs/runs?'+p);$('rl').innerHTML='<tr><th><input type="checkbox" onclick="document.querySelectorAll(\'.ck\').forEach(c=>c.checked=this.checked)"></th><th>Thời gian</th><th>Tên</th><th>Loại</th><th>Input</th><th>Output</th><th>Trễ</th><th>Token</th><th>Chi phí</th><th>Tag</th><th>Feedback</th></tr>'+
 rs.map(r=>`<tr><td><input type="checkbox" class="ck" value="${r.id}"></td><td class="r" onclick="openT('${r.trace_id}')">${vn(r.start_time)}</td><td class="r" onclick="openT('${r.trace_id}')"><b>${esc(r.name)}</b>${r.status==='error'?' <span class="tag bl">lỗi</span>':''}</td><td>${esc(r.run_type)}</td><td>${esc((r.inputs||'').slice(0,90))}</td><td>${esc((r.error||r.outputs||'').slice(0,90))}</td>
 <td>${r.latency_ms!=null?(r.latency_ms/1000).toFixed(2)+'s':'…'}</td><td>${r.total_tokens??''}</td><td>${usd(r.cost_usd)}</td><td>${(r.tags||[]).slice(0,3).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</td><td>${(r.feedback||[]).map(f=>`<span class="tag ro">${esc(f.key)}:${f.score??''}</span>`).join('')}</td></tr>`).join('')||'<tr><td>Không có run khớp</td></tr>'}catch(e){flash(e.message,1)}}
$('go').onclick=runs;$('subag').onchange=runs;$('flt').onkeydown=e=>{if(e.key==='Enter')runs()};
const checked=()=>[...document.querySelectorAll('.ck:checked')].map(c=>c.value);
$('tods').onclick=async()=>{const r=await api('api/datasets/'+$('bulkds').value+'/from_runs','POST',{run_ids:checked()});flash('Dataset: '+r.results.join(', '))};
$('toq').onclick=async()=>{await api('api/queues/'+$('bulkq').value+'/items','POST',{run_ids:checked()});flash('Đã thêm vào hàng đợi')};
let CUR=null;
async function openT(tid){const d=await api('api/obs/traces/'+tid);CUR=d;const root=d.runs.find(r=>!r.parent_id)||d.runs[0];const b=$('trv');b.style.display='block';
 b.innerHTML=`<div class="row"><h3 style="margin:0">Trace ${esc(tid.slice(0,8))} · ${esc(root.name)}</h3><span style="flex:1"></span><button class="s" id="shb">Chia sẻ link</button><button class="s" id="fbb">+ Feedback</button></div><div id="shr"></div>
 <div class="kpi" style="margin:8px 0"><div>Thời gian<b>${((root.latency_ms||0)/1000).toFixed(2)}s</b></div><div>Run<b>${d.runs.length}</b></div><div>Token<b>${d.runs.filter(r=>r.run_type==='llm').reduce((a,r)=>a+(r.total_tokens||0),0)||root.total_tokens||0}</b></div><div>Chi phí<b>${usd(root.cost_usd)}</b></div><div>Thread<b style="font-size:12px">${esc(root.thread_id||'–')}</b></div></div>
 <div class="grid2"><div>${treeHtml(d.runs,d.feedback)}<div id="atts" style="margin-top:8px"></div><div class="row"><input type="file" id="attf"><button class="s" id="attb">Đính kèm vào run đang chọn</button></div></div><div id="rd"></div></div>`;
 const att=async()=>{const a=await api('api/obs/traces/'+tid+'/attachments');$('atts').innerHTML=a.length?'<b>File đính kèm</b> '+a.map(x=>`<a class="tag" href="api/obs/attachments/${x.id}" target="_blank"><svg class="ic"><use href="#i-paperclip"/></svg> ${esc(x.name)} (${Math.ceil(x.size/1024)} KB)</a>`).join(' '):''};att();
 $('attb').onclick=async()=>{const f=$('attf').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);try{await api('api/obs/runs/'+(window.SEL||root.id)+'/attachments','POST',fd);att();flash('Đã đính kèm')}catch(e){flash(e.message,1)}};
 sel(root.id);$('shb').onclick=async()=>{const s=await api('api/obs/share','POST',{trace_id:tid,hours:72});const u=location.origin+s.path;$('shr').innerHTML=`<p>Link chỉ-đọc (hết hạn sau 72 giờ, thu hồi được ở Alerts & Rules): <input style="width:70%" value="${esc(u)}" onclick="this.select()"></p>`};
 $('fbb').onclick=async()=>{const k=prompt('Khoá feedback (vd dung):','dung');if(!k)return;const sc=prompt('Điểm (số):','1');await api('api/obs/feedback','POST',{run_id:root.id,key:k,score:sc===''?null:+sc});openT(tid)};b.scrollIntoView({behavior:'smooth'})}
function sel(id){const r=CUR.runs.find(x=>x.id===id);if(!r)return;window.SEL=id;$('rd').innerHTML=`<h4 style="margin:0">${esc(r.name)} <small>(${esc(r.run_type)})</small></h4><small>${vn(r.start_time)} · ${r.latency_ms} ms · ${esc(r.model||'')} ${r.input_tokens!=null?'· '+r.input_tokens+'→'+r.output_tokens+' tok':''}</small>
 ${r.error?`<pre class="err">${esc(r.error)}</pre>`:''}<b>Input</b><pre>${esc(JSON.stringify(r.inputs,null,1))}</pre><b>Output</b><pre>${esc(JSON.stringify(r.outputs,null,1))}</pre><b>Metadata</b><pre>${esc(JSON.stringify(r.metadata))}</pre><small>tags: ${(r.tags||[]).map(esc).join(', ')}</small>`}
async function threads(){const ts=await api('api/obs/projects/'+P+'/threads');$('tl').innerHTML='<tr><th>Thread</th><th>Lượt</th><th>Bắt đầu</th><th>Gần nhất</th><th>Token</th><th>Chi phí</th><th>Lỗi</th><th>Câu cuối</th></tr>'+
 ts.map(t=>`<tr class="r" onclick="thread('${esc(t.thread_id).replace(/'/g,'')}')"><td>${esc(t.thread_id)}</td><td>${t.turns}</td><td>${vn(t.first)}</td><td>${vn(t.last)}</td><td>${t.tokens??''}</td><td>${usd(t.cost)}</td><td>${t.errors}</td><td>${esc(t.last_input)}</td></tr>`).join('')}
async function thEval(tid){if(!$('tev').value)return flash('Chọn evaluator',1);$('thr').textContent='Đang chấm…';try{const r=await api(`api/obs/projects/${P}/threads/${encodeURIComponent(tid)}/evaluate`,'POST',{evaluator_id:+$('tev').value});$('thr').innerHTML=r.map(x=>`<b>${esc(x.key)}</b>: ${x.score??esc(x.value)} — ${esc(x.comment||'')}`).join('<br>')}catch(e){$('thr').textContent=e.message}}
async function thread(tid){const rs=await api('api/obs/projects/'+P+'/threads/'+encodeURIComponent(tid));$('td').innerHTML=`<div class="row"><h3 style="margin:0">Thread ${esc(tid)}</h3><button class="s" onclick="thEval('${esc(tid).replace(/'/g,'')}')">Chấm cả thread</button> <a href="studio?agent=${(rs[0]&&rs[0].trace_id)?'':''}" style="display:none"></a></div><p id="thr"></p>`+rs.map(r=>`<div class="card" style="margin:6px 0"><small>${vn(r.start_time)} · ${(r.latency_ms/1000).toFixed(1)}s · <a href="#" onclick="openT('${r.trace_id}');return false">trace</a></small>
 <div style="background:#eff6ff;padding:6px;border-radius:6px;margin:4px 0"><svg class="ic"><use href="#i-user"/></svg> ${esc(r.inputs?.message||r.inputs?.question||JSON.stringify(r.inputs))}</div><div style="padding:6px"><svg class="ic"><use href="#i-bot"/></svg> ${esc(r.error||r.outputs?.answer||JSON.stringify(r.outputs))}</div></div>`).join('')}
async function dash(){const d=await api(`api/obs/projects/${P}/dashboard?hours=${$('dh').value}&bucket=${$('db').value}`),t=d.total;
 $('dk').innerHTML=[['Trace',t.n],['Lỗi',t.n?`${t.errors} (${(t.errors/t.n*100).toFixed(1)}%)`:0],['p50',t.p50?(t.p50/1000).toFixed(2)+'s':'–'],['p95',t.p95?(t.p95/1000).toFixed(2)+'s':'–'],['Token',t.tokens],['Chi phí',usd(t.cost)]].map(([k,v])=>`<div>${k}<b>${esc(v)}</b></div>`).join('');
 const s=d.series.map(x=>({...x,err_rate:x.n?x.errors/x.n*100:0,p50s:(x.p50||0)/1000,p95s:(x.p95||0)/1000}));
 chart($('c1'),s,'n',{title:'Số trace',bar:true});chart($('c2'),s,'err_rate',{title:'Tỉ lệ lỗi %',color:'#dc2626',fmt:v=>v.toFixed(1)+'%'});
 chart($('c3'),s,'p50s',{title:'Độ trễ p50 (s)',fmt:v=>v.toFixed(2)+'s'});chart($('c4'),s,'p95s',{title:'Độ trễ p95 (s)',color:'#9333ea',fmt:v=>v.toFixed(2)+'s'});
 chart($('c5'),s,'tokens',{title:'Token',bar:true,color:'#0891b2'});chart($('c6'),s,'cost',{title:'Chi phí (USD)',color:'#15803d',fmt:v=>'$'+Number(v).toFixed(5)});
 $('dl').innerHTML='<tr><th>Model</th><th>Gọi</th><th>Lỗi</th><th>p50</th><th>Token</th><th>Chi phí</th></tr>'+d.llm.map(x=>`<tr><td>${esc(x.model)}</td><td>${x.calls}</td><td>${x.errors}</td><td>${Math.round(x.p50||0)} ms</td><td>${x.tokens}</td><td>${usd(x.cost)}</td></tr>`).join('');
 $('dt').innerHTML='<tr><th>Tool</th><th>Gọi</th><th>Lỗi</th><th>p50</th></tr>'+d.tools.map(x=>`<tr><td>${esc(x.name)}</td><td>${x.calls}</td><td>${x.errors}</td><td>${Math.round(x.p50||0)} ms</td></tr>`).join('');
 $('df').innerHTML='<tr><th>Khoá</th><th>Số</th><th>TB</th></tr>'+d.feedback.map(x=>`<tr><td>${esc(x.key)}</td><td>${x.n}</td><td>${x.avg==null?'–':Number(x.avg).toFixed(2)}</td></tr>`).join('')}
$('dgo').onclick=dash;
async function setup(){const ks=await api('api/obs/projects/'+P+'/keys');$('kt').innerHTML='<tr><th>Prefix</th><th>Nhãn</th><th>Môi trường</th><th>Tạo</th><th></th></tr>'+ks.map(k=>`<tr><td>${esc(k.prefix)}…</td><td>${esc(k.label)}</td><td>${k.environment?envB(k.environment):'<small>theo dự án</small>'}</td><td>${vn(k.created)}</td><td>${k.revoked?'đã thu hồi':`<button class="s" onclick="rk(${k.id})">Thu hồi</button>`}</td></tr>`).join('')}
async function rk(id){await api('api/obs/keys/'+id,'DELETE');setup()}
$('kb').onclick=async()=>{const r=await api('api/obs/projects/'+P+'/keys?label='+encodeURIComponent($('kl').value)+'&environment='+encodeURIComponent($('ke').value),'POST');$('knew').innerHTML=`<pre>${esc(r.api_key)}</pre><small>${esc(r.note)}</small>`;setup()};
$('peb').onclick=async()=>{await api('api/obs/projects/'+P,'PATCH',{environment:$('pe').value});$('penv').innerHTML=envB($('pe').value);flash('Đã lưu môi trường');plist()};
$('retb').onclick=async()=>{await api('api/obs/projects/'+P,'PATCH',{retention_days:+$('ret').value});flash('Đã lưu retention')};
$('npb').onclick=async()=>{await api('api/obs/projects','POST',{name:$('np').value,environment:$('npe').value});plist()};
const TRACE0=new URLSearchParams(location.search).get('trace');
(async()=>{await plist();const ds=await api('api/datasets');$('bulkds').innerHTML=ds.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('');
 const evs0=await api('api/evaluators');$('tev').innerHTML=evs0.filter(e=>e.kind==='llm').map(e=>`<option value="${e.id}" ${e.name==='hoi_thoai_dat_muc_tieu'?'selected':''}>${esc(e.name)}</option>`).join('');
 const qs=await api('api/queues');$('bulkq').innerHTML=qs.map(q=>`<option value="${q.id}">${esc(q.name)}</option>`).join('');
 const ps=await api('api/obs/projects');const want=new URLSearchParams(location.search).get('p');const p=ps.find(x=>x.id==want)||ps[0];if(p)await openP(p.id,p.name,p.retention_days,p.environment);
 })().catch(e=>console.error('init',e)).finally(()=>{const t=TRACE0;if(t)openT(t).then(()=>$('trv').scrollIntoView())});""")

RULES = page("rules", "Alerts & Rules", """<main>
<div class="card"><h2>Alerts</h2><p style="font-size:13px">Kiểm tra mỗi ~60 giây. Kênh: <b>webhook</b> (chỉ tự gửi tới địa chỉ nội bộ / private), <b>email_draft</b> (chỉ tạo bản nháp trong Outbox, không tự gửi ra ngoài), <b>log</b>.</p>
<div class="row"><select id="ap"></select><input id="an" placeholder="tên cảnh báo"><select id="am"><option>error_rate</option><option>latency_p95</option><option>latency_p50</option><option>count</option><option>cost</option><option>tokens</option><option>feedback_avg</option></select>
<input id="afk" placeholder="feedback key" style="width:110px"><select id="ao"><option>gt</option><option>gte</option><option>lt</option><option>lte</option></select><input id="at" type="number" step="any" placeholder="ngưỡng" style="width:90px">
<input id="aw" type="number" value="15" style="width:70px" title="cửa sổ phút"> phút<select id="ac"><option>log</option><option>telegram</option><option>email</option><option>slack</option><option>webhook</option><option>email_draft</option></select><input id="atg" placeholder="chat_id / email nhận / URL"><input id="asc" placeholder="tên bí mật (token bot / URL Slack / khoá ký)" style="width:210px"><input id="ash" placeholder="SMTP host:port:user (email)" style="width:170px"><input id="asp" placeholder="tên bí mật mật khẩu SMTP" style="width:170px"><input id="af" placeholder="bộ lọc (tuỳ chọn)" style="font-family:monospace"><button id="ab">Tạo</button></div>
<table id="al" style="margin-top:8px"></table></div>
<div class="card"><h2>Automation rules</h2><p style="font-size:13px">Áp lên trace gốc mới (mỗi ~20 giây), theo bộ lọc + tỉ lệ lấy mẫu. Hành động: thêm vào dataset, vào annotation queue, chạy online evaluator, gắn tag, webhook nội bộ.</p>
<div class="row"><select id="rp"></select><input id="rn" placeholder="tên rule"><input id="rf" placeholder='bộ lọc, vd eq(status,"error")' style="font-family:monospace;flex:1"><input id="rs" type="number" min="0" max="1" step="0.05" value="1" style="width:70px" title="sampling">
<select id="rt"><option value="add_to_dataset">→ dataset</option><option value="add_to_queue">→ annotation queue</option><option value="run_evaluator">chạy evaluator (online)</option><option value="add_tag">gắn tag</option></select><select id="rx"></select><input id="rtag" placeholder="tag" style="width:90px">
<input id="rbf" type="number" value="0" min="0" style="width:70px" title="backfill giờ"> giờ backfill<button id="rb">Tạo</button></div><table id="rl" style="margin-top:8px"></table></div>
<div class="card"><h2>Ẩn dữ liệu nhạy cảm (server-side redaction)</h2><p style="font-size:13px">Regex áp lên input/output/lỗi của MỌI run trước khi lưu (SDK, OTLP, agent). Thay thế hỗ trợ nhóm <code>\\1</code>.</p>
<div class="row"><input id="rdn" placeholder="tên"><input id="rdp" placeholder="regex" style="flex:1;font-family:monospace"><input id="rdr" value="[ĐÃ ẨN]" style="width:140px"><button class="s" id="rdb">Thêm</button></div>
<div class="row" style="margin-top:6px"><input id="rdt" placeholder="thử: dán đoạn văn có key/mật khẩu" style="flex:1"><button class="s" id="rdtb">Thử</button><code id="rdo"></code></div><table id="rdl" style="margin-top:6px"></table></div>
<div class="grid2"><div class="card"><h2>Outbox (bản nháp & webhook)</h2><table id="ob"></table></div><div class="card"><h2>Link chia sẻ trace</h2><table id="sh"></table></div></div>
<div class="card"><h3>Worker nền</h3><pre id="wk"></pre></div></main>""", r"""
let ds=[],qs=[],evs=[];
function fillX(){const t=$('rt').value;const L=t==='add_to_dataset'?ds:t==='add_to_queue'?qs:t==='run_evaluator'?evs:[];$('rx').innerHTML=L.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');$('rx').style.display=L.length?'':'none';$('rtag').style.display=t==='add_tag'?'':'none'}
async function red(){const r=await api('api/obs/redaction');$('rdl').innerHTML='<tr><th>Tên</th><th>Regex</th><th>Thay bằng</th><th></th></tr>'+r.map(x=>`<tr><td>${esc(x.name)}</td><td><code>${esc(x.pattern)}</code></td><td>${esc(x.replacement)}</td><td><button class="s" onclick="api('api/obs/redaction/${x.id}?enabled=${!x.enabled}','PATCH').then(red)">${x.enabled?'Tắt':'Bật'}</button> <button class="s" onclick="api('api/obs/redaction/${x.id}','DELETE').then(red)">Xoá</button></td></tr>`).join('')}
$('rdb').onclick=async()=>{try{await api('api/obs/redaction','POST',{name:$('rdn').value||'rule',pattern:$('rdp').value,replacement:$('rdr').value});red()}catch(e){flash(e.message,1)}};
$('rdtb').onclick=async()=>{const r=await api('api/obs/redaction/test','POST',{text:$('rdt').value});$('rdo').textContent=r.output};red();
async function load(){const a=await api('api/obs/alerts');$('al').innerHTML='<tr><th>Tên</th><th>Dự án</th><th>Điều kiện</th><th>Kênh</th><th>Giá trị gần nhất</th><th>Lần bắn</th><th></th></tr>'+a.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.project)}</td><td><code>${esc(x.metric)}${x.feedback_key?'('+esc(x.feedback_key)+')':''} ${x.op} ${x.threshold}</code> / ${x.window_min}p ${x.filter?'<br><small>'+esc(x.filter)+'</small>':''}</td><td>${esc(x.channel)} ${esc(x.target||'')}</td><td>${x.last_value??'–'}</td><td><small>${(x.events||[]).map(e=>vn(e.ts)+' = '+Number(e.value).toPrecision(3)+' → '+esc(e.delivery)).join('<br>')||'–'}</small></td><td><button class="s" onclick="ta(${x.id})">Thử</button> <button class="s" onclick="ta(${x.id},1)">Gửi thử</button> <button class="s" onclick="da(${x.id})">Xoá</button></td></tr>`).join('');
 const r=await api('api/obs/rules');$('rl').innerHTML='<tr><th>Tên</th><th>Dự án</th><th>Bộ lọc</th><th>Sampling</th><th>Hành động</th><th>Khớp / áp dụng</th><th>Nhật ký</th><th></th></tr>'+r.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.project)}</td><td><code>${esc(x.filter||'(tất cả)')}</code></td><td>${x.sampling_rate}</td><td>${esc(JSON.stringify(x.actions))}</td><td>${x.n_matched} / ${x.n_applied}</td><td><small>${(x.log||[]).map(l=>esc(l.action+': '+l.result)).join('<br>')}</small></td><td><button class="s" onclick="tr_(${x.id},${!x.enabled})">${x.enabled?'Tắt':'Bật'}</button> <button class="s" onclick="dr(${x.id})">Xoá</button></td></tr>`).join('');
 const o=await api('api/obs/outbox').catch(()=>[]);$('ob').innerHTML='<tr><th>Lúc</th><th>Loại</th><th>Tới</th><th>Tiêu đề</th><th>Trạng thái</th></tr>'+o.map(x=>`<tr><td>${vn(x.created)}</td><td>${esc(x.kind)}</td><td>${esc(x.target||'')}</td><td title="${esc(x.body)}">${esc(x.subject)}</td><td><span class="tag ${x.status==='sent'?'ro':''}">${esc(x.status)}</span></td></tr>`).join('');
 const s=await api('api/obs/shares');$('sh').innerHTML='<tr><th>ID</th><th>Trace</th><th>Hết hạn</th><th>Lượt xem</th><th></th></tr>'+s.map(x=>`<tr><td>${esc(x.id)}</td><td><a href="projects?trace=${x.trace_id}">${esc(String(x.trace_id).slice(0,8))}</a></td><td>${vn(x.expires)}</td><td>${x.views}</td><td>${x.revoked?'đã thu hồi':`<button class="s" onclick="rv('${x.id}')">Thu hồi</button>`}</td></tr>`).join('');
 $('wk').textContent=JSON.stringify(await api('api/obs/worker'),null,1)}
async function ta(id,send){const r=await api(`api/obs/alerts/${id}/test${send?'?send=true':''}`,'POST');flash(send?`đã gửi thử → ${r.delivery}`:`giá trị = ${r.value} → ${r.fires?'SẼ BẮN':'không bắn'}`)}
async function da(id){await api('api/obs/alerts/'+id,'DELETE');load()}async function dr(id){await api('api/obs/rules/'+id,'DELETE');load()}
async function tr_(id,en){await api(`api/obs/rules/${id}?enabled=${en}`,'PATCH');load()}async function rv(id){await api('api/obs/share/'+id,'DELETE');load()}
function alertCfg(){const k=$('ac').value,c={},s=$('asc').value.trim();if(k==='telegram'||k==='slack')c.secret=s;if(k==='webhook'&&s)c.signing_secret=s;
 if(k==='email'){const [h,p,u]=$('ash').value.split(':').map(x=>x.trim());if(h)c.host=h;if(p)c.port=+p;if(u)c.user=u;if($('asp').value.trim())c.password='{{secret:'+$('asp').value.trim()+'}}'}return c}
$('ab').onclick=async()=>{try{await api('api/obs/alerts','POST',{project_id:+$('ap').value,name:$('an').value,metric:$('am').value,feedback_key:$('afk').value||null,op:$('ao').value,threshold:+$('at').value,window_min:+$('aw').value,channel:$('ac').value,target:$('atg').value||null,config:alertCfg(),filter:$('af').value||null});load()}catch(e){flash(e.message,1)}};
$('rb').onclick=async()=>{const t=$('rt').value;const a={type:t};if(t==='add_to_dataset')a.dataset_id=+$('rx').value;if(t==='add_to_queue')a.queue_id=+$('rx').value;if(t==='run_evaluator')a.evaluator_id=+$('rx').value;if(t==='add_tag')a.tag=$('rtag').value;
 try{await api('api/obs/rules','POST',{project_id:+$('rp').value,name:$('rn').value,filter:$('rf').value||null,sampling_rate:+$('rs').value,actions:[a],backfill_hours:+$('rbf').value});load()}catch(e){flash(e.message,1)}};
$('rt').onchange=fillX;
(async()=>{const ps=await api('api/obs/projects');const o=ps.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');$('ap').innerHTML=o;$('rp').innerHTML=o;ds=await api('api/datasets');qs=await api('api/queues');evs=await api('api/evaluators');fillX();load()})();""")

DATASETS = page("datasets", "Datasets", """<main><div class="card"><div class="row"><h2 style="margin:0">Datasets</h2><span style="flex:1"></span><input id="dn" placeholder="tên dataset"><input id="dd" placeholder="mô tả"><button id="db">Tạo</button></div><table id="dl"></table></div>
<div class="card" id="dv" style="display:none"></div></main>""", r"""
let D=null;
async function list(){const d=await api('api/datasets');$('dl').innerHTML='<tr><th>#</th><th>Tên</th><th>Mô tả</th><th>Ví dụ</th><th>Phiên bản</th><th>Tag</th><th>Experiment</th><th></th></tr>'+d.map(x=>`<tr><td>${x.id}</td><td class="r" onclick="open_(${x.id})"><b>${esc(x.name)}</b></td><td>${esc(x.description)}</td><td>${x.n}</td><td>v${x.version}</td><td>${(x.tags||[]).map(t=>`<span class="tag">${esc(t.tag)}=v${t.version}</span>`).join('')}</td><td><a href="experiments?dataset=${x.id}">${x.n_experiments}</a></td><td><button class="s" onclick="del_(${x.id})">Xoá</button></td></tr>`).join('')}
async function del_(id){if(confirm('Xoá dataset?')){await api('api/datasets/'+id,'DELETE');list();$('dv').style.display='none'}}
async function open_(id,ver,split){D=id;const p=new URLSearchParams();if(ver)p.set('version',ver);if(split)p.set('split',split);const d=await api(`api/datasets/${id}?`+p);const b=$('dv');b.style.display='block';
 b.innerHTML=`<div class="row"><h2 style="margin:0">${esc(d.dataset.name)}</h2><span class="tag">đang xem v${d.version}</span><span style="flex:1"></span>
 Phiên bản <select id="vv"><option value="">mới nhất (v${d.dataset.version})</option>${d.history.map(h=>`<option value="${h.version}" ${h.version==ver?'selected':''}>v${h.version} · ${vn(h.ts)} (${h.changes} thay đổi)</option>`).join('')}</select>
 Split <select id="ss"><option value="">tất cả</option>${d.splits.map(s=>`<option ${s===split?'selected':''}>${esc(s)}</option>`).join('')}</select>
 <input id="tg" placeholder="tag (vd prod)" style="width:100px"><button class="s" id="tgb">Gắn tag cho v${d.version}</button></div>
 <table style="margin-top:8px"><tr><th>#</th><th>Inputs</th><th>Outputs tham chiếu</th><th>Split</th><th>Nguồn</th><th></th></tr>${d.examples.map(e=>`<tr><td>${e.id}<br><small>v${e.version}</small></td><td><pre>${esc(JSON.stringify(e.inputs,null,1))}</pre></td><td><pre>${esc(JSON.stringify(e.outputs,null,1))}</pre></td><td>${(e.splits||[]).map(s=>`<span class="tag">${esc(s)}</span>`).join('')}</td><td>${e.source_run_id?`<a href="projects?trace=${e.source_run_id}" target="_blank">run ${esc(String(e.source_run_id).slice(0,8))}</a>`:'thủ công'}</td>
 <td><button class="s" onclick='edit_(${JSON.stringify(e).replace(/'/g,"&#39;")})'>Sửa</button> <button class="s" onclick="dex(${e.id})">Xoá</button><br><span id="att${e.id}"></span> <label class="s" style="cursor:pointer;font-size:12px"><svg class="ic"><use href="#i-paperclip"/></svg><input type="file" style="display:none" onchange="exAtt(${e.id},this)"></label></td></tr>`).join('')}</table>
 <h3>Thêm ví dụ</h3><div class="grid2"><div><label>Inputs (JSON)</label><textarea id="ei" rows="3">{"question": ""}</textarea></div><div><label>Outputs tham chiếu (JSON)</label><textarea id="eo" rows="3">{"answer": ""}</textarea></div></div>
 <div class="row"><input id="esp" value="base" placeholder="split (cách nhau dấu phẩy)"><input type="hidden" id="eid"><button id="eb">Lưu ví dụ</button><button class="g" id="ec" onclick="$('eid').value='';$('eb').textContent='Lưu ví dụ'">Huỷ sửa</button></div>
 <details><summary>Nhập CSV</summary><textarea id="csv" rows="4" placeholder="question,answer&#10;Đổ rác ngày nào?,Thứ Ba và thứ Sáu"></textarea><div class="row"><input id="ci" value="question" placeholder="cột input"><input id="co" value="answer" placeholder="cột output"><input id="cs" value="base"><button class="s" id="cb">Nhập</button></div></details>`;
 d.examples.forEach(e=>loadAtt(e.id));
 $('vv').onchange=()=>open_(id,$('vv').value,$('ss').value);$('ss').onchange=()=>open_(id,$('vv').value,$('ss').value);
 $('tgb').onclick=async()=>{await api(`api/datasets/${id}/tags`,'POST',{tag:$('tg').value,version:d.version});flash('Đã gắn tag');list()};
 $('eb').onclick=async()=>{try{const body={inputs:JSON.parse($('ei').value),outputs:JSON.parse($('eo').value||'null'),splits:$('esp').value.split(',').map(s=>s.trim()).filter(Boolean)};
  if($('eid').value)await api('api/examples/'+$('eid').value,'PUT',body);else await api(`api/datasets/${id}/examples`,'POST',[body]);open_(id);list()}catch(e){flash(e.message,1)}};
 $('cb').onclick=async()=>{const r=await api(`api/datasets/${id}/import_csv`,'POST',{csv:$('csv').value,input_cols:$('ci').value.split(','),output_cols:$('co').value?$('co').value.split(','):[],split:$('cs').value});flash(`Đã nhập ${r.ids.length} ví dụ`);open_(id);list()}}
function edit_(e){$('ei').value=JSON.stringify(e.inputs);$('eo').value=JSON.stringify(e.outputs);$('esp').value=(e.splits||[]).join(',');$('eid').value=e.id;$('eb').textContent='Lưu (tạo phiên bản mới)';$('ei').scrollIntoView()}
async function exAtt(eid,inp){const fd=new FormData();fd.append('file',inp.files[0]);try{await api('api/examples/'+eid+'/attachments','POST',fd);loadAtt(eid);flash('Đã đính kèm')}catch(e){flash(e.message,1)}}
async function loadAtt(eid){const a=await api('api/examples/'+eid+'/attachments');const x=$('att'+eid);if(x)x.innerHTML=a.map(f=>`<a href="api/example_attachments/${f.id}"><svg class="ic"><use href="#i-file-text"/></svg>${esc(f.name)}</a>`).join(' ')}
async function dex(id){await api('api/examples/'+id,'DELETE');open_(D);list()}
$('db').onclick=async()=>{try{await api('api/datasets','POST',{name:$('dn').value,description:$('dd').value});list()}catch(e){flash(e.message,1)}};
list().then(()=>{const d=new URLSearchParams(location.search).get('d');if(d)open_(+d)});""")

EXPERIMENTS = page("experiments", "Experiments", f"""<style>.win{{background:#dcfce7}}.lose{{background:#fee2e2}}</style><main>
<div class="card"><h2>Chạy experiment</h2><div class="row"><input id="xn" placeholder="tên experiment"><select id="xd"></select><input id="xv" type="number" placeholder="phiên bản dataset" style="width:150px"><input id="xs" placeholder="split" style="width:90px">
<select id="xt"><option value="agent">Agent</option><option value="prompt">Prompt (hub)</option></select><select id="xa"></select><input id="xav" type="number" placeholder="phiên bản" style="width:90px">
<input id="xr" type="number" value="1" min="1" max="5" style="width:60px" title="lặp lại"> lần</div>
<div style="margin-top:6px">Evaluators: <span id="xe"></span></div><button id="xb" style="margin-top:6px">Chạy</button></div>
<div class="card"><div class="row"><h2 style="margin:0">Experiments</h2><select id="fd"><option value="">mọi dataset</option></select><span style="flex:1"></span><button id="cmp" class="g">So sánh đã chọn</button><button id="pw" class="g">Pairwise (2 đã chọn)</button></div><table id="xl"></table></div>
<div class="card" id="cv" style="display:none"></div><div class="card"><h2>Pairwise</h2><table id="pl"></table><div id="pd"></div></div>
<div class="card"><h2>Lịch chạy eval (cron)</h2><div class="row"><input id="sn" placeholder="tên lịch"><input id="sc" value="0 7 * * *" style="font-family:monospace;width:120px" title="phút giờ ngày tháng thứ"><small>dùng dataset/target/evaluator ở form trên</small><button id="sb" class="s">Tạo lịch</button></div><table id="sl"></table></div></main>""", r"""
let agents=[],prompts=[],evs=[];
function tgt(){const t=$('xt').value;return t==='agent'?{type:'agent',agent_id:+$('xa').value,version:$('xav').value?+$('xav').value:null}:{type:'prompt',prompt_id:+$('xa').value,version:$('xav').value?+$('xav').value:null}}
function fillT(){const L=$('xt').value==='agent'?agents:prompts;$('xa').innerHTML=L.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}
const evIds=()=>[...document.querySelectorAll('.xev:checked')].map(c=>+c.value);
async function list(){const f=$('fd').value;const xs=await api('api/experiments'+(f?'?dataset_id='+f:''));
 $('xl').innerHTML='<tr><th></th><th>#</th><th>Tên</th><th>Dataset</th><th>Target</th><th>Trạng thái</th><th>Điểm TB</th><th>Lỗi</th><th>p95</th><th>Token</th><th>Chi phí</th><th></th></tr>'+xs.map(x=>`<tr><td><input type="checkbox" class="xc" value="${x.id}"></td><td>${x.id}</td><td><b>${esc(x.name)}</b><br><small>${vn(x.created)}${x.schedule_id?' · lịch #'+x.schedule_id:''}</small></td><td>${esc(x.dataset)} v${x.dataset_version??'?'}${x.split?' ['+esc(x.split)+']':''}</td>
 <td><small translate="no">${esc(JSON.stringify(x.target))}</small></td><td>${esc(x.status)} ${esc(x.progress||'')}</td><td>${x.summary.scores.map(s=>`<span class="tag ro">${esc(s.key)}: ${s.avg}</span>`).join('')}${Object.entries(x.summary.summary_scores||{}).map(([k,v])=>`<span class="tag" style="background:#ede9fe" title="${esc(v.comment||'')}">Σ ${esc(k)}: ${v.score??'–'}</span>`).join('')}</td><td>${x.summary.errors}/${x.summary.n}</td><td>${x.summary.p95?(x.summary.p95/1000).toFixed(1)+'s':'–'}</td><td>${x.summary.tokens??''}</td><td>${usd(x.summary.cost)}</td><td><button class="s" onclick="cmp_('${x.id}')">Xem</button> <button class="s" onclick="dx(${x.id})">Xoá</button></td></tr>`).join('');
 if(xs.some(x=>x.status==='running'||x.status==='queued'))setTimeout(list,4000)}
async function dx(id){if(confirm('Xoá experiment?')){await api('api/experiments/'+id,'DELETE');list()}}
async function cmp_(ids){const d=await api('api/experiments/compare?ids='+ids);const X=d.experiments;const keys=[...new Set(d.results.flatMap(r=>(r.fb||[]).map(f=>f.key)))];
 const cell=(e,x)=>d.results.filter(r=>r.experiment_id===e.id&&r.example_id===x.id);const avg=(rs,k)=>{const v=rs.flatMap(r=>(r.fb||[]).filter(f=>f.key===k&&f.score!=null).map(f=>f.score));return v.length?v.reduce((a,b)=>a+b,0)/v.length:null};
 const b=$('cv');b.style.display='block';b.innerHTML=`<h2>So sánh: ${X.map(e=>'#'+e.id+' '+esc(e.name)).join(' · ')}</h2>
 <table><tr><th>Chỉ số</th>${X.map(e=>`<th>#${e.id} ${esc(e.name)}</th>`).join('')}</tr>${keys.map(k=>{const v=X.map(e=>d.summary[e.id].scores.find(s=>s.key===k)?.avg);const best=Math.max(...v.filter(x=>x!=null).map(Number));return `<tr><td>${esc(k)}</td>${v.map(x=>`<td class="${x!=null&&Number(x)===best&&X.length>1?'win':''}">${x??'–'}</td>`).join('')}</tr>`}).join('')}
 <tr><td>Lỗi</td>${X.map(e=>`<td>${d.summary[e.id].errors}/${d.summary[e.id].n}</td>`).join('')}</tr><tr><td>Trễ TB</td>${X.map(e=>`<td>${d.summary[e.id].avg_ms} ms</td>`).join('')}</tr><tr><td>Chi phí</td>${X.map(e=>`<td>${usd(d.summary[e.id].cost)}</td>`).join('')}</tr></table>
 <h3>Từng ví dụ</h3><table><tr><th>Input</th><th>Tham chiếu</th>${X.map(e=>`<th>#${e.id}</th>`).join('')}</tr>${d.examples.map(x=>{const per=X.map(e=>cell(e,x));const sc=per.map(rs=>keys.length?avg(rs,keys[0]):null);const best=Math.max(...sc.filter(v=>v!=null));
 return `<tr><td>${esc(JSON.stringify(x.inputs))}</td><td><small>${esc(JSON.stringify(x.outputs))}</small></td>${per.map((rs,i)=>`<td class="${X.length>1&&sc[i]!=null&&sc[i]===best&&sc.filter(v=>v===best).length<sc.length?'win':''}">${rs.map(r=>`<div>${esc(((r.output||{}).answer||r.error||'').slice(0,300))}<br>${(r.fb||[]).map(f=>`<span class="tag ro" title="${esc(f.comment||'')}">${esc(f.key)}:${f.score??esc(f.value)}</span>`).join('')} <small>${r.latency_ms}ms</small> ${r.run_id?`<a href="projects?trace=${r.run_id}" target="_blank">trace</a>`:''}</div>`).join('<hr>')||'…'}</td>`).join('')}</tr>`}).join('')}</table>`;b.scrollIntoView({behavior:'smooth'})}
$('cmp').onclick=()=>{const ids=[...document.querySelectorAll('.xc:checked')].map(c=>c.value);if(ids.length)cmp_(ids.join(','))};
$('pw').onclick=async()=>{const ids=[...document.querySelectorAll('.xc:checked')].map(c=>+c.value);if(ids.length!==2)return flash('Chọn đúng 2 experiment',1);const r=await api('api/pairwise','POST',{exp_a:ids[0],exp_b:ids[1]});flash('Đang chạy pairwise #'+r.id);setTimeout(pl,3000)};
async function pl(){const p=await api('api/pairwise');$('pl').innerHTML='<tr><th>#</th><th>A</th><th>B</th><th>Trạng thái</th><th>Kết quả</th><th></th></tr>'+p.map(x=>`<tr><td>${x.id}</td><td>#${x.exp_a}</td><td>#${x.exp_b}</td><td>${esc(x.status)}</td><td>${x.summary?`A thắng ${x.summary.A} · B thắng ${x.summary.B} · hoà ${x.summary.tie}`:''}</td><td><button class="s" onclick="pd(${x.id})">Chi tiết</button></td></tr>`).join('');if(p.some(x=>x.status==='running'))setTimeout(pl,4000)}
async function pd(id){const d=await api('api/pairwise/'+id);$('pd').innerHTML='<table><tr><th>Ví dụ</th><th>Thắng</th><th>Đảo thứ tự?</th><th>Lý do</th></tr>'+d.results.map(r=>`<tr><td>#${r.example_id}</td><td><b>${r.winner}</b></td><td>${r.swapped?'có':'không'}</td><td>${esc(r.reasoning)}</td></tr>`).join('')+'</table>'}
$('xb').onclick=async()=>{try{const r=await api('api/experiments','POST',{name:$('xn').value||'exp',dataset_id:+$('xd').value,dataset_version:$('xv').value?+$('xv').value:null,split:$('xs').value||null,target:tgt(),evaluator_ids:evIds(),repetitions:+$('xr').value});flash('Đã bắt đầu #'+r.id);list()}catch(e){flash(e.message,1)}};
async function sl(){const s=await api('api/schedules');$('sl').innerHTML='<tr><th>Tên</th><th>Cron</th><th>Dataset</th><th>Target</th><th>Lần chạy gần nhất</th><th></th></tr>'+s.map(x=>`<tr><td>${esc(x.name)}</td><td><code>${esc(x.cron)}</code></td><td>${esc(x.dataset)}</td><td><small translate="no">${esc(JSON.stringify(x.target))}</small></td><td>${vn(x.last_run)} ${x.last_experiment?'→ #'+x.last_experiment:''}</td><td><button class="s" onclick="sn_(${x.id})">Chạy ngay</button> <button class="s" onclick="st_(${x.id},${!x.enabled})">${x.enabled?'Tắt':'Bật'}</button> <button class="s" onclick="sd(${x.id})">Xoá</button></td></tr>`).join('')}
async function sn_(id){const r=await api(`api/schedules/${id}/run_now`,'POST');flash('Experiment #'+r.experiment_id);list();sl()}async function st_(id,e){await api(`api/schedules/${id}?enabled=${e}`,'PATCH');sl()}async function sd(id){await api('api/schedules/'+id,'DELETE');sl()}
$('sb').onclick=async()=>{try{await api('api/schedules','POST',{name:$('sn').value,cron:$('sc').value,dataset_id:+$('xd').value,split:$('xs').value||null,target:tgt(),evaluator_ids:evIds()});sl()}catch(e){flash(e.message,1)}};
$('xt').onchange=fillT;$('fd').onchange=list;
(async()=>{agents=await api('api/agents');prompts=await api('api/prompts');evs=await api('api/evaluators');const ds=await api('api/datasets');
 $('xd').innerHTML=ds.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('');$('fd').insertAdjacentHTML('beforeend',ds.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join(''));
 const w=new URLSearchParams(location.search).get('dataset');if(w){$('fd').value=w;$('xd').value=w}
 $('xe').innerHTML=evs.map(e=>`<label style="display:inline-block;margin-right:10px"><input type="checkbox" class="xev" value="${e.id}"> ${esc(e.name)} <small>(${e.kind})</small></label>`).join('');fillT();list();pl();sl()})();""")

EVALUATORS = page("evaluators", "Evaluators", """<main><div class="grid2"><div class="card"><h2>Evaluators</h2><table id="el"></table></div>
<div class="card"><h2 id="eh">Tạo evaluator</h2><div class="row"><input id="en" placeholder="tên (a-z0-9_-)"><select id="ek"><option value="llm">LLM-as-judge</option><option value="code">Code (Python, sandbox)</option><option value="summary">Summary (cả experiment)</option></select><input id="ekey" placeholder="khoá điểm"></div>
<div id="llmf"><label>Rubric (biến {{input}} {{output}} {{reference}} {{trajectory}} — trajectory = chuỗi tool agent đã gọi)</label><textarea id="ep" rows="6"></textarea><div class="row"><select id="est"><option value="numeric">số</option><option value="boolean">đúng/sai</option><option value="categorical">phân loại</option></select>
<input id="emin" type="number" value="1" style="width:60px"><input id="emax" type="number" value="5" style="width:60px"><input id="ech" placeholder="lựa chọn, cách nhau dấu phẩy"></div>
<label>Ví dụ chấm mẫu (few-shot, JSON list – tuỳ chọn)</label><textarea id="efs" rows="2" placeholder='[{"output":"...","score":5,"reasoning":"..."}]'></textarea></div>
<div id="codef" style="display:none"><label>Code — định nghĩa <code>def evaluate(run, example)</code>; trả bool / số / {"score","comment"} / list nhiều điểm. Chạy trong sandbox Docker (không mạng, 256 MB, timeout 20s). <code>run["trajectory"]</code> = danh sách tool đã gọi (name/args/ok). Summary: <code>def summarize(results)</code> nhận mọi kết quả (inputs, reference, output, error, scores).</label><textarea id="ec" rows="10" style="font-family:monospace">def evaluate(run, example):
    out = str(run.get("output") or "")
    return {"score": 1 if len(out) < 300 else 0, "comment": f"{len(out)} ký tự"}</textarea></div>
<div class="row" style="margin-top:8px"><button id="es">Lưu</button><input type="hidden" id="eid"></div>
<h3>Thử nhanh</h3><div class="grid2"><div><label>run (JSON)</label><textarea id="tr" rows="3">{"input":"Đổ rác ngày nào?","output":"Tối thứ Ba và thứ Sáu lúc 19h"}</textarea></div><div><label>example (JSON)</label><textarea id="tx" rows="3">{"outputs":{"answer":"Tối thứ Ba và thứ Sáu lúc 19h"}}</textarea></div></div><button id="tb" class="g">Thử</button><pre id="to"></pre></div></div></main>""", r"""
let EV=[];
function mode(){const c=$('ek').value!=='llm';$('llmf').style.display=c?'none':'';$('codef').style.display=c?'':'none'}
async function list(){EV=await api('api/evaluators');$('el').innerHTML='<tr><th>Tên</th><th>Loại</th><th>Khoá</th><th></th></tr>'+EV.map(e=>`<tr><td><b>${esc(e.name)}</b></td><td>${e.kind}</td><td>${esc(e.config.key)}</td><td><button class="s" onclick="ed(${e.id})">Sửa/thử</button> <button class="s" onclick="de(${e.id})">Xoá</button></td></tr>`).join('')}
function ed(id){const e=EV.find(x=>x.id===id),c=e.config;$('eid').value=id;$('eh').textContent='Sửa '+e.name;$('en').value=e.name;$('ek').value=e.kind;$('ekey').value=c.key;mode();
 if(e.kind!=='llm')$('ec').value=c.code;else{$('ep').value=c.prompt;$('est').value=c.score_type||'numeric';$('emin').value=c.min??1;$('emax').value=c.max??5;$('ech').value=(c.choices||[]).join(',');$('efs').value=c.few_shot?JSON.stringify(c.few_shot):''}}
async function de(id){if(confirm('Xoá?')){await api('api/evaluators/'+id,'DELETE');list()}}
function cfg(){const k=$('ek').value;if(k!=='llm')return {key:$('ekey').value,code:$('ec').value};const c={key:$('ekey').value,prompt:$('ep').value,score_type:$('est').value,min:+$('emin').value,max:+$('emax').value};
 if($('ech').value)c.choices=$('ech').value.split(',').map(s=>s.trim());if($('efs').value)c.few_shot=JSON.parse($('efs').value);return c}
$('es').onclick=async()=>{try{const b={name:$('en').value,kind:$('ek').value,config:cfg()};if($('eid').value)await api('api/evaluators/'+$('eid').value,'PUT',b);else await api('api/evaluators','POST',b);flash('Đã lưu');list()}catch(e){flash(e.message,1)}};
$('tb').onclick=async()=>{if(!$('eid').value)return flash('Lưu trước rồi thử',1);$('to').textContent='…';try{$('to').textContent=JSON.stringify(await api(`api/evaluators/${$('eid').value}/test`,'POST',{run:JSON.parse($('tr').value),example:JSON.parse($('tx').value||'null')}),null,1)}catch(e){$('to').textContent='Lỗi: '+e.message}};
$('ek').onchange=mode;list();""")

ANNOTATION = page("annotation", "Annotation", """<main><div class="grid2"><div class="card"><h2>Hàng đợi chú thích</h2><table id="ql"></table>
<details><summary>+ Tạo hàng đợi</summary><input id="qn" placeholder="tên" style="width:100%"><label>Hướng dẫn</label><textarea id="qi" rows="2"></textarea>
<label>Rubric (JSON list: key, type numeric|boolean|categorical, min/max/options, description)</label><textarea id="qr" rows="5">[{"key":"dung","type":"numeric","min":1,"max":5,"description":"Đúng dữ liệu?"},{"key":"huu_ich","type":"boolean","description":"Hữu ích?"}]</textarea><button id="qb">Tạo</button></details></div>
<div class="card" id="rv"><h2>Chọn một hàng đợi để bắt đầu duyệt</h2></div></div></main>""", r"""
let Q=null;
async function list(){const q=await api('api/queues');$('ql').innerHTML='<tr><th>Tên</th><th>Chờ</th><th>Xong</th><th></th></tr>'+q.map(x=>`<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.description||'')}</small></td><td>${x.pending}</td><td>${x.done}</td><td><button class="s" onclick="start(${x.id})">Duyệt</button></td></tr>`).join('')}
async function start(id){Q=id;const d=await api(`api/queues/${id}/next`);const b=$('rv');
 if(!d.item){b.innerHTML=`<h2>${esc(d.queue.name)}</h2><p>No items left to review.</p>`;list();return}const r=d.run||{};
 b.innerHTML=`<h2>${esc(d.queue.name)} <small>(còn ${d.pending})</small></h2><p><small>${esc(d.queue.instructions||'')}</small></p>
 <div style="background:#eff6ff;padding:8px;border-radius:6px"><svg class="ic"><use href="#i-user"/></svg> ${esc(r.inputs?.message||r.inputs?.question||JSON.stringify(r.inputs))}</div>
 <div style="padding:8px;white-space:pre-wrap"><svg class="ic"><use href="#i-bot"/></svg> ${esc(r.error||r.outputs?.answer||JSON.stringify(r.outputs))}</div><small>${vn(r.start_time)} · ${r.latency_ms} ms · <a href="projects?trace=${r.trace_id}" target="_blank">xem trace</a></small>
 ${d.queue.rubric.map(c=>`<label><b>${esc(c.key)}</b> – ${esc(c.description||'')}</label>${c.type==='numeric'?`<div class="row">${Array.from({length:(c.max||5)-(c.min||1)+1},(_,i)=>(c.min||1)+i).map(v=>`<label><input type="radio" name="r_${c.key}" value="${v}"> ${v}</label>`).join(' ')}</div>`:c.type==='boolean'?`<div class="row"><label><input type="radio" name="r_${c.key}" value="true"> Có</label><label><input type="radio" name="r_${c.key}" value="false"> Không</label></div>`:`<select id="r_${c.key}"><option value="">–</option>${(c.options||[]).map(o=>`<option>${esc(o)}</option>`).join('')}</select>`}`).join('')}
 <label>Ghi chú</label><textarea id="cm" rows="2"></textarea><label>Câu trả lời đúng (sửa lại – tuỳ chọn)</label><textarea id="cr" rows="2"></textarea>
 <div class="row" style="margin-top:8px"><button id="ok">Lưu & tiếp</button><button class="g" id="sk">Bỏ qua</button></div>`;
 const sc=()=>{const o={};d.queue.rubric.forEach(c=>{if(c.type==='categorical')o[c.key]=$('r_'+c.key).value;else{const x=document.querySelector(`input[name="r_${c.key}"]:checked`);if(x)o[c.key]=x.value}});return o};
 $('ok').onclick=async()=>{await api('api/queue_items/'+d.item.id,'POST',{scores:sc(),comment:$('cm').value||null,correction:$('cr').value||null});start(id);list()};
 $('sk').onclick=async()=>{await api('api/queue_items/'+d.item.id,'POST',{scores:{},action:'skip'});start(id);list()}}
$('qb').onclick=async()=>{try{await api('api/queues','POST',{name:$('qn').value,instructions:$('qi').value,rubric:JSON.parse($('qr').value)});list()}catch(e){flash(e.message,1)}};
list();""")

def share_page(runs, expires):
    data = json.dumps(runs, ensure_ascii=False).replace("</", "<\\/")
    return (f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>Trace chia sẻ</title><style>{CSS}{TREE_CSS}</style></head><body><nav><b>Moza Agent OS</b><span style="color:#9ca3af">· trace chia sẻ chỉ-đọc · hết hạn {html.escape(str(expires))[:16]}</span></nav>'
            f'<main><div class="card"><div class="grid2"><div id="tree"></div><div id="rd"></div></div></div></main><script>'
            "function esc(s){return String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]))}"
            "const usd=v=>v==null?'–':'$'+Number(v).toFixed(6);const $=id=>document.getElementById(id);"
            f"const RUNS={data};" + CHART_JS +
            "const CUR={runs:RUNS};function sel(id){const r=RUNS.find(x=>x.id===id);$('rd').innerHTML=`<h4>${esc(r.name)}</h4><small>${r.latency_ms} ms</small>${r.error?`<pre>${esc(r.error)}</pre>`:''}<b>Input</b><pre>${esc(JSON.stringify(r.inputs,null,1))}</pre><b>Output</b><pre>${esc(JSON.stringify(r.outputs,null,1))}</pre>`}"
            "$('tree').innerHTML=treeHtml(RUNS,[]);sel((RUNS.find(r=>!r.parent_id)||RUNS[0]).id);</script></body></html>")

PAGES = {"/projects": PROJECTS, "/rules": RULES, "/datasets": DATASETS, "/experiments": EXPERIMENTS, "/evaluators": EVALUATORS, "/annotation": ANNOTATION}
