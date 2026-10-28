"""GD7.3 Insights page (/insights): list of reports + topic tree (share bars, error rate, score, cost, examples). Data: /api/insights/*."""
from ui import page
from ui_ls import COMMON_JS

CSS = """<style>
.ph{padding:16px 20px 0}.ins{display:grid;grid-template-columns:320px 1fr;min-height:calc(100vh - 110px);border-top:1px solid var(--bd2);margin-top:12px}
.ins>aside{border-right:1px solid var(--bd2);overflow:auto}.ins>section{padding:16px 20px;min-width:0;overflow:auto}
.rp{padding:10px 14px;border-bottom:1px solid var(--bd3);cursor:pointer}.rp:hover{background:var(--s2)}.rp.on{background:var(--sel)}.rp .s{font-size:12px;color:var(--t3);margin-top:3px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tp{border:1px solid var(--bd2);border-radius:var(--r-lg);background:var(--s1);margin-bottom:10px}.tph,.sub,.thd{display:grid;grid-template-columns:minmax(240px,2.2fr) minmax(110px,1fr) 56px 56px 72px;gap:12px;align-items:center}.tph>div:first-child,.thd>div:first-child{min-width:0}
.tph{padding:10px 14px;cursor:pointer}.tph:hover{background:var(--s2)}.tph .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tph .sm{font-size:12px;color:var(--t3);font-weight:400;white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.bar{height:8px;border-radius:99px;background:var(--s3);overflow:hidden}.bar>i{display:block;height:100%;background:var(--brand);border-radius:99px}.sub .bar>i{opacity:.6}
.tpb{border-top:1px solid var(--bd3);padding:8px 14px 12px;display:none}.tp.open .tpb{display:block}.sub{padding:6px 0 6px 18px;font-size:13px}
.ex{display:block;padding:4px 0 4px 18px;font-size:12.5px;color:var(--t2);text-decoration:none}.ex:hover{color:var(--brand)}.dl{font-size:12px;margin-left:6px}.dl.up{color:var(--ok)}.dl.dn{color:var(--er)}
.thd{padding:0 14px 6px;font-size:11px;color:var(--t3)}
.fd{border:1px solid var(--bd2);border-radius:var(--r-lg);background:var(--s1);padding:10px 14px;margin-bottom:8px}.fd b{display:block;margin-bottom:2px}
.np label{display:block;margin:12px 0 4px;font-size:12px;color:var(--t3)}.np textarea,.np input{width:100%;box-sizing:border-box}.np .row{display:flex;gap:8px;margin-top:12px}.at{font-size:12px;color:var(--t3);padding:2px 0 2px 18px}
</style>"""

JS = COMMON_JS + r"""
const T_=(en,vi)=>LANG==='vi'?vi:en, QS=new URLSearchParams(location.search);
const usdI=v=>'$'+Number(v||0).toFixed(Number(v||0)<0.01&&v>0?4:3), pct=v=>(v*100).toFixed(v>0&&v<0.1?1:0)+'%';
let REPS=[],CUR=null,POLL=null,PROJS=[],CFGS=[];
const stChip=s=>`<span class="chip ${s==='done'?'ok':s==='failed'?'er':'wn'}">${s==='done'?T_('done','xong'):s==='failed'?T_('failed','lỗi'):T_('running','đang chạy')}</span>`;
function delta(c){if(c.prev_share==null)return'';const d=(c.share-c.prev_share)*100;if(Math.abs(d)<1)return'';return `<span class="dl ${d>0?'up':'dn'}" title="${T_('vs previous report','so với báo cáo trước')}">${d>0?'▲':'▼'}${Math.abs(d).toFixed(0)}${T_(' pts',' đ%')}</span>`}
const cols=c=>`<div><div class="bar"><i style="width:${Math.max(1,c.share*100)}%"></i></div><small class="muted">${c.n} · ${pct(c.share)}</small></div>
 <div>${errPill(c.error_rate)}</div><div>${c.avg_score==null?'<span class="muted">–</span>':Number(c.avg_score).toFixed(2)}</div><div class="mono">${usdI(c.cost_usd)}</div>`;
const exLinks=c=>c.examples.map(e=>`<a class="ex" href="projects?p=${e.project_id}&trace=${e.trace_id}" title="${esc(e.thread_id)}">${ICN('messages-square')} ${esc(e.text||e.thread_id)} ${e.status==='error'?'<span class="chip er">error</span>':''}</a>`).join('')
 +(c.hidden_examples?`<div class="ex muted">${ICN('lock')} ${T_(c.hidden_examples+' private example(s) hidden',c.hidden_examples+' ví dụ riêng tư đã ẩn')}</div>`:'');
const attrTxt=a=>esc(a.name)+': '+(a.type==='boolean'?pct(a.true_share)+' '+T_('true','đúng'):a.type==='number'?Number(a.avg).toFixed(1):(a.top||[]).map(x=>esc(x[0])+' ('+x[1]+')').join(', '));
const attrHtml=c=>(c.attrs&&c.attrs.length)?`<div class="at">${c.attrs.map(attrTxt).join(' · ')}</div>`:'';
const findHtml=r=>(r.findings&&r.findings.length)?`<h3 style="margin:0 0 8px">${T_('Findings','Phát hiện chính')}</h3>`+r.findings.map(f=>`<div class="fd"><b>${esc(f.title)}</b><div>${esc(f.detail)}</div>${f.traces.map(t=>`<a class="ex" href="projects?p=${t.project_id}&trace=${t.trace_id}" title="${esc(t.trace_id)}">${ICN('messages-square')} ${esc(t.text||t.trace_id)} ${t.status==='error'?'<span class="chip er">error</span>':''}</a>`).join('')}${f.hidden?`<div class="ex muted">${ICN('lock')} ${T_(f.hidden+' private trace(s) hidden',f.hidden+' trace riêng tư đã ẩn')}</div>`:''}</div>`).join('')+'<div style="margin-bottom:14px"></div>':'';
function topicHtml(c){return `<div class="tp" data-id="${c.id}"><div class="tph"><div><div class="nm">${esc(c.label)}${delta(c)}</div><div class="sm">${esc(c.summary||'')}</div></div>${cols(c)}</div>
 <div class="tpb">${attrHtml(c)}${c.children.map(s=>`<div class="sub"><div><b>${esc(s.label)}</b><div class="muted" style="font-size:12px">${esc(s.summary||'')}</div></div>${cols(s)}</div>${exLinks(s)}`).join('')}${exLinks(c)}</div></div>`}
async function showReport(id){CUR=id;clearTimeout(POLL);drawList();const r=await api('api/insights/reports/'+id),m=r.meta||{};
 let h=`<div class="phead"><h2 style="margin:0">${esc(r.period)}</h2>${stChip(r.status)}<span class="sp"></span><span class="muted">${r.scope==='workspace'?T_('All projects','Tất cả project'):esc(r.scope)}</span><button class="s" id="ask">${ICN('sparkles')} ${T_('Ask the assistant about this report','Hỏi trợ lý về báo cáo này')}</button></div>
  <p>${esc(r.summary||'')}</p><div class="kpi" style="margin-bottom:16px"><div>${T_('Conversations','Hội thoại')}<b>${r.n_threads}</b></div><div>${T_('Topics','Chủ đề')}<b>${r.clusters.length}</b></div><div>${T_('Report cost','Chi phí báo cáo')}<b>${usdI(r.cost_usd)}</b></div>${m.sampled?`<div>${T_('Sampled','Lấy mẫu')}<b>${T_('newest 1000','1000 mới nhất')}</b></div>`:''}</div>`;
 if(r.status==='running'){h+=`<div class="muted">${ICN('loader')} ${T_('Analysing…','Đang phân tích…')}</div>`;POLL=setTimeout(()=>{loadList().then(()=>showReport(id))},2500)}
 else if(r.status==='done')h+=(r.notes?`<div class="muted" style="margin-bottom:10px">${esc(r.notes)}</div>`:'')+findHtml(r)+`<div class="thd"><div>${T_('Topic','Chủ đề')}</div><div>${T_('Share','Tỷ lệ')}</div><div>${T_('Errors','Lỗi')}</div><div>${T_('Score','Điểm')}</div><div>${T_('Cost','Chi phí')}</div></div>`+r.clusters.map(topicHtml).join('');
 $('rv').innerHTML=h;document.querySelectorAll('.tph').forEach(x=>x.onclick=()=>x.parentElement.classList.toggle('open'));
 $('ask').onclick=()=>{window.AOS_CTX={report_id:String(id)};const b=document.getElementById('askbtn');if(b)b.click()}}
function drawList(){$('rl').innerHTML=REPS.length?REPS.map(r=>`<div class="rp ${r.id===CUR?'on':''}" data-id="${r.id}"><div style="display:flex;gap:8px;align-items:center"><b>${esc(r.period)}</b><span class="sp"></span>${stChip(r.status)}</div><div class="s">${esc(r.summary||'')}</div><div class="muted" style="font-size:11px;margin-top:3px">${rel(r.created_at)} · ${r.n_threads} ${T_('conv.','hội thoại')}${r.source==='weekly'?' · '+T_('weekly','hằng tuần'):''}</div></div>`).join('')
 :emptyState('chart-column',T_('No reports yet','Chưa có báo cáo'),T_('Run an analysis to see what people use your agents for.','Chạy phân tích để xem mọi người dùng agent vào việc gì.'));
 document.querySelectorAll('.rp').forEach(x=>x.onclick=()=>showReport(+x.dataset.id))}
async function loadList(){REPS=await api('api/insights/reports');drawList()}
async function loadCfgs(){CFGS=await api('api/insights/configs');$('cfg').innerHTML=`<option value="">${T_('Quick run (topics)','Chạy nhanh (chủ đề)')}</option>`+CFGS.map(c=>`<option value="${c.id}">${esc(c.name)} · ${c.mode==='traces'?'traces':T_('topics','chủ đề')}</option>`).join('')}
function newReport(){CUR=null;clearTimeout(POLL);drawList();$('rv').innerHTML=`<div class="np"><h2 style="margin:0 0 4px">${T_('New report','Báo cáo mới')}</h2><label>${T_('What do you want to know?','Bạn muốn biết điều gì?')}</label>
 <textarea id="nq" rows="2" placeholder="${T_('e.g. What do customers ask most and where does the agent fail?','vd: Khách hay hỏi gì nhất và agent thất bại ở đâu?')}"></textarea><div class="row"><button id="ng">${ICN('sparkles')} ${T_('Draft the report','Soạn báo cáo')}</button></div><div id="nd"></div></div>`;
 $('ng').onclick=async()=>{const b=$('ng');b.disabled=true;try{const p=$('prj').value;drawDraft(await api('api/insights/configs/generate','POST',{question:$('nq').value,project_ids:p?[+p]:[],lang:LANG==='vi'?'vi':'en'}))}catch(e){flash(e.message,1)}b.disabled=false}}
function drawDraft(d){$('nd').innerHTML=`<label>${T_('Name','Tên')}</label><input id="dn" value="${esc(d.name)}"><label>${T_('What to extract from each trace','Cần rút gì từ mỗi trace')}</label><textarea id="dp" rows="4">${esc(d.summary_prompt)}</textarea>
 <label>${T_('Attributes to measure (one per line: name | type | description; type = string, number, boolean, category)','Thuộc tính cần đo (mỗi dòng: tên | kiểu | mô tả; kiểu = string, number, boolean, category)')}</label><textarea id="da" rows="4">${esc(d.attributes.map(a=>[a.name,a.type,a.description].join(' | ')).join('\n'))}</textarea>
 <div class="row"><button class="s" id="ds">${T_('Save','Lưu')}</button><button id="dr">${ICN('play')} ${T_('Save & run','Lưu và chạy')}</button></div>`;
 $('ds').onclick=()=>saveDraft(false);$('dr').onclick=()=>saveDraft(true)}
async function saveDraft(run){try{const p=$('prj').value,attrs=$('da').value.split('\n').map(l=>l.split('|').map(x=>x.trim())).filter(a=>a[0]).map(a=>({name:a[0],type:a[1]||'string',description:a[2]||''}));
 const c=await api('api/insights/configs','POST',{name:$('dn').value,mode:'traces',project_ids:p?[+p]:[],range_days:+$('days').value,sample_size:200,summary_prompt:$('dp').value,attributes:attrs,lang:LANG==='vi'?'vi':'en'});
 await loadCfgs();$('cfg').value=c.id;flash(T_('Saved','Đã lưu'));if(run){const r=await api('api/insights/configs/'+c.id+'/run','POST');await loadList();showReport(r.id)}}catch(e){flash(e.message,1)}}
async function runNow(){const b=$('run');b.disabled=true;try{if($('cfg').value){const r=await api('api/insights/configs/'+$('cfg').value+'/run','POST');await loadList();showReport(r.id);b.disabled=false;return}
 const r=await api('api/insights/run','POST',{days:+$('days').value,project_id:$('prj').value?+$('prj').value:null,lang:LANG==='vi'?'vi':'en'});await loadList();showReport(r.id)}catch(e){flash(e.message,1)}b.disabled=false}
async function drawSet(){const s=await api('api/insights/settings');$('wk').innerHTML=`<label class="muted" style="display:flex;gap:6px;align-items:center" title="${T_('Sent to the alert channels of this workspace','Gửi tới kênh cảnh báo của workspace')}"><input type="checkbox" id="wkc" ${s.weekly?'checked':''}> ${T_('Weekly report (Mon 08:00)','Báo cáo hằng tuần (T2 08:00)')}</label>`;
 $('wkc').onchange=async e=>{try{await api('api/insights/settings','PUT',{weekly:e.target.checked});flash(T_('Saved','Đã lưu'))}catch(x){e.target.checked=!e.target.checked;flash(x.message,1)}}}
(async()=>{PROJS=await api('api/obs/projects');
 $('hd').innerHTML=`<div class="phead"><span class="ic-tile">${ICN('chart-column')}</span><h1>Insights</h1><span class="muted">${T_('What people use your agents for','Mọi người dùng agent vào việc gì')}</span><span class="sp"></span><span id="wk"></span>
  <select id="cfg" title="${T_('Saved report configurations','Cấu hình báo cáo đã lưu')}"></select><button class="s" id="nr">${ICN('plus')} ${T_('New report','Báo cáo mới')}</button>
  <select id="prj"><option value="">${T_('All projects','Tất cả project')}</option>${PROJS.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
  <select id="days"><option value="7">${T_('Last 7 days','7 ngày qua')}</option><option value="30">${T_('Last 30 days','30 ngày qua')}</option><option value="90">${T_('Last 90 days','90 ngày qua')}</option></select>
  <button id="run">${ICN('play')}${T_('Run analysis','Chạy phân tích')}</button></div>`;
 $('run').onclick=runNow;$('nr').onclick=newReport;loadCfgs().catch(()=>{});drawSet().catch(()=>{});await loadList();const q=+QS.get('report')||(REPS[0]&&REPS[0].id);if(q)showReport(q)})().catch(e=>flash(e.message,1));
"""

INSIGHTS = page("insights", "Insights", CSS + """<div class="ph" id="hd"></div><div class="ins"><aside id="rl"></aside><section id="rv"><div class="muted"></div></section></div>""", JS)
PAGES = {"/insights": INSIGHTS}
