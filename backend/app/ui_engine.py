"""GD7.4 Engine page (/engine): issues list (filters status/kind) + issue detail (root cause, evidence -> traces, proposal diff, actions, re-test result). Data: /api/engine/*. VI/EN via T_()."""
from ui import page
from ui_ls import COMMON_JS

CSS = """<style>
.ph{padding:16px 20px 0}.eng{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 110px);border-top:1px solid var(--bd2);margin-top:12px}
.eng>aside{border-right:1px solid var(--bd2);overflow:auto}.eng>section{padding:16px 20px;min-width:0;overflow:auto}
.is{padding:10px 14px;border-bottom:1px solid var(--bd3);cursor:pointer}.is:hover{background:var(--s2)}.is.on{background:var(--sel)}
.is .t{font-weight:600;font-size:13px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}.is .m{font-size:11.5px;color:var(--t3);margin-top:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cd{border:1px solid var(--bd2);border-radius:var(--r-lg);background:var(--s1);padding:12px 14px;margin-bottom:12px}.cd h3{margin:0 0 6px;font-size:13px;color:var(--t2);font-weight:600}
.qt{border-left:3px solid var(--brand);padding:4px 10px;margin:6px 0;font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.df{font-family:var(--mono);font-size:12px;white-space:pre-wrap;max-height:320px;overflow:auto;background:var(--s2);border-radius:var(--r-md);padding:8px}.df .a{color:var(--ok)}.df .d{color:var(--er)}
.tb{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tb input[type=text]{min-width:200px;flex:1}.hist{font-size:12.5px;margin:0;padding:0;list-style:none}.hist li{padding:4px 0;border-bottom:1px solid var(--bd3)}.sv0{background:var(--er);color:#fff}.sv1{color:var(--er)}.fl{display:flex;gap:8px;flex-wrap:wrap;padding:8px 14px;border-bottom:1px solid var(--bd3)}.fl select{flex:1;min-width:90px}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.cmp td,.cmp th{padding:5px 10px;font-size:12.5px;text-align:left}
</style>"""

JS = COMMON_JS + r"""
const T_=(en,vi)=>LANG==='vi'?vi:en, QS=new URLSearchParams(location.search);
const KIND={error_cluster:['Error cluster','Cụm lỗi'],low_score:['Low score','Điểm thấp'],negative_feedback:['Thumbs-down','Không hài lòng'],inefficiency:['Inefficiency','Kém hiệu quả'],truncated_tool_result:['Truncated tool result','Kết quả công cụ bị cắt'],guard_violation:['Guard violation','Vi phạm rào chắn'],manual:['Reported by hand','Báo thủ công']};
const STAT={open:['open','mới','wn'],fixing:['in progress','đang xử lý','wn'],watching:['watching','theo dõi','wn'],completed:['fixed','đã sửa','ok'],ignored:['ignored','bỏ qua','']};
const kindT=k=>T_(...(KIND[k]||[k,k])), stChip=s=>{const x=STAT[s]||[s,s,''];return `<span class="chip ${x[2]}">${T_(x[0],x[1])}</span>`};
const usdE=v=>'$'+Number(v||0).toFixed(Number(v||0)>0&&v<0.01?4:3), fmtD=t=>t?new Date(t).toLocaleString(LANG==='vi'?'vi-VN':'en-US',{timeZone:'Asia/Ho_Chi_Minh'}):'–';
const SEV=[['Urgent','Khẩn cấp'],['High','Cao'],['Medium','Trung bình'],['Low','Thấp']], CLOSE_R={resolved:['Resolved','Đã giải quyết'],not_important:['Not important','Không quan trọng'],incorrectly_flagged:['Incorrectly flagged','Báo nhầm'],not_reproduced:['Not reproduced','Không tái hiện được'],duplicate:['Duplicate','Trùng']};
const sevT=n=>T_(...(SEV[n]||['?','?'])), sevChip=n=>`<span class="chip sv${n}" title="${T_('Priority','Mức ưu tiên')}">${sevT(n)}</span>`;
let LIST=[],CNT={},CUR=null,POLL=null,BUSY=false,ME={},CATS=new Set();
const VK='aos_engine_view_'+(QS.get('p')||'all'), VIEW=(()=>{try{return JSON.parse(localStorage.getItem(VK))||{}}catch(e){return {}}})();
const saveView=()=>{VIEW.fs=$('fs').value;VIEW.fk=$('fk').value;VIEW.fp=$('fp').value;VIEW.fc=$('fc').value;VIEW.so=$('so').value;try{localStorage.setItem(VK,JSON.stringify(VIEW))}catch(e){}};
const canEdit=()=>ME.role&&ME.role!=='viewer'&&!ME.demo;
async function loadList(){saveView();const p=new URLSearchParams({status:$('fs').value,kind:$('fk').value,sort:$('so').value});if($('fp').value!=='')p.set('severity',$('fp').value);if($('fc').value)p.set('category',$('fc').value);
 const d=await api('api/engine/issues?'+p);LIST=d.items;CNT=d.counts;LIST.forEach(i=>i.category&&CATS.add(i.category));const c=$('fc');if(c){const cur=c.value;c.innerHTML=`<option value="">${T_('All categories','Mọi nhóm')}</option>`+[...CATS].sort().map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');c.value=cur}drawList()}
function drawList(){$('il').innerHTML=LIST.length?LIST.map(i=>`<div class="is ${i.id===CUR?'on':''}" data-id="${i.id}"><div class="t">${esc(i.title)}</div><div class="m">${stChip(i.status)}${sevChip(i.severity)}<span class="chip">${kindT(i.kind)}</span><span>×${i.count}</span><span>${rel(i.last_seen)}</span>${i.reopened?`<span class="chip er" title="${T_('came back after being fixed','tái diễn sau khi đã sửa')}">${ICN('rotate-ccw')}${i.reopened}</span>`:''}</div></div>`).join('')
 :emptyState('circle-check',T_('No issues','Không có vấn đề nào'),T_('The detector looks for recurring errors, low evaluator scores and thumbs-down every 10 minutes. Press "Scan now" to check immediately.','Hệ thống quét lỗi lặp lại, điểm đánh giá thấp và lượt không hài lòng mỗi 10 phút. Bấm "Quét ngay" để kiểm tra ngay.'));
 document.querySelectorAll('.is').forEach(x=>x.onclick=()=>show(+x.dataset.id))}
const diffHtml=d=>esc(d).split('\n').map(l=>l.startsWith('+')&&!l.startsWith('+++')?`<span class="a">${l}</span>`:l.startsWith('-')&&!l.startsWith('---')?`<span class="d">${l}</span>`:l).join('\n');
function expCol(x){if(!x)return `<td colspan="5" class="muted">–</td>`;const s=x.summary||{},sc=(s.scores||[]).map(r=>`${esc(r.key)} ${r.avg}`).join(', ')||'–';
 return `<td>${esc(x.status)}${x.progress?' '+esc(x.progress):''}</td><td>${s.n??'–'}</td><td>${s.errors??'–'}</td><td>${s.avg_ms??'–'}</td><td>${sc}</td>`}
function retestHtml(rs){if(!rs.length)return '';const r=rs[rs.length-1];
 return `<div class="cd"><h3>${T_('Re-test result (old vs new commit on the dataset)','Kết quả chạy lại (commit cũ so với mới trên dataset)')}</h3><table class="cmp"><thead><tr><th></th><th>${T_('Status','Trạng thái')}</th><th>${T_('Examples','Ví dụ')}</th><th>${T_('Errors','Lỗi')}</th><th>${T_('Avg ms','TB ms')}</th><th>${T_('Scores','Điểm')}</th></tr></thead>
 <tbody><tr><td><b>${T_('Old','Cũ')}</b></td>${expCol(r.old)}</tr><tr><td><b>${T_('New','Mới')}</b></td>${expCol(r.new)}</tr></tbody></table></div>`}
// ---- 12.35 Validation (Test issue): per-trace verdicts of the replays + links to the replay runs
const VST={confirmed:['Reproduced','Tái hiện được','er'],not_reproduced:['Not reproduced','Không tái hiện được','ok'],inconclusive:['Inconclusive','Chưa kết luận','wn'],running:['Running…','Đang chạy…','wn'],error:['Failed','Lỗi','er']},
 VVD={reproduced:['reproduced','tái hiện được','er'],not_reproduced:['not reproduced','không tái hiện','ok'],inconclusive:['inconclusive','chưa kết luận','wn']};
const vChip=(m,k)=>{const x=m[k]||[k,k,''];return `<span class="chip ${x[2]}">${T_(x[0],x[1])}</span>`};
function validationHtml(v){const b=v&&v.baseline;if(!b)return '';
 const rows=(b.traces||[]).map(t=>t.hidden?`<div class="qt muted">${ICN('lock')} ${vChip(VVD,t.verdict)} ${T_('private conversation hidden','hội thoại riêng tư đã ẩn')}</div>`
  :`<div class="qt">${vChip(VVD,t.verdict)} ${esc(t.reason||'')}${(t.blocked||[]).length?` <span class="muted">${T_('blocked in the test','bị chặn khi chạy thử')}: ${t.blocked.map(x=>`<span class="chip">${esc(x)}</span>`).join(' ')}</span>`:''}${t.error&&!t.reason?` <span class="err">${esc(t.error)}</span>`:''}
   ${t.replay_run_id&&b.replay_project_id?`<a class="evl" href="projects?p=${b.replay_project_id}&trace=${t.replay_run_id}">${ICN('external-link')} ${T_('replay run','lần chạy lại')}</a>`:''}</div>`).join('');
 return `<div class="cd" id="vb"><h3>${T_('Validation','Kiểm chứng')} ${vChip(VST,b.status)}</h3>${b.status==='error'?`<span class="err">${esc(b.error||'')}</span>`:''}${rows}
  <small class="muted">${b.agent_version?T_('Agent version','Phiên bản agent')+' v'+b.agent_version+' · ':''}${b.tested_at?fmtD(b.tested_at)+' · ':''}${b.cost_usd!=null?usdE(b.cost_usd)+' · ':''}${T_('The test replays the conversation without sending, saving or changing anything; approvals are rejected.','Lần chạy thử phát lại hội thoại mà không gửi, lưu hay đổi gì; mọi yêu cầu duyệt đều bị từ chối.')}</small></div>`}
// ---- 12.36 Fix verification: attempts (one experiment each) + evidence table: reported trace / reproduced on the original / after the fix
const FST={fix_verified:['Fix verified','Sửa đã kiểm chứng','ok'],recurred:['Problem recurred','Vấn đề tái diễn','er'],inconclusive:['Inconclusive','Chưa kết luận','wn'],running:['Running…','Đang chạy…','wn'],error:['Failed','Lỗi','er']};
function fixHtml(d,tr){const v=d.validation||{},f=v.fix,b=v.baseline;if(!f)return '';
 const at=f.attempts||[],last=at[at.length-1]||{},by=(x,id)=>((x&&x.traces)||[]).find(t=>t.run_id===id),ids=[...new Set([...(b?b.traces:[]),...(last.traces||[])].map(t=>t.run_id).filter(Boolean))];
 const rows=ids.map(id=>{const s=tr[id],o=by(b,id),n=by(last,id);return `<tr><td>${s?`<a class="evl" href="projects?p=${s.project_id}&trace=${s.trace_id}">${esc(s.text||id.slice(0,8))}</a>`:esc(id.slice(0,8))}</td><td>${o?vChip(VVD,o.verdict):'–'}</td><td>${n?vChip(VVD,n.verdict):'–'}</td></tr>`}).join('')
  +(last.traces||[]).filter(t=>t.hidden).map(t=>`<tr><td class="muted">${ICN('lock')} ${T_('private conversation hidden','hội thoại riêng tư đã ẩn')}</td><td>–</td><td>${vChip(VVD,t.verdict)}</td></tr>`).join('');
 return `<div class="cd" id="fxb"><h3>${T_('Fix verification','Kiểm chứng cách sửa')} ${vChip(FST,f.status)}</h3>${f.status==='error'?`<span class="err">${esc(f.error||'')}</span>`:''}${f.message?`<p class="${f.status==='fix_verified'?'':'err'}">${esc(f.message)}</p>`:''}
  ${rows?`<table class="cmp"><thead><tr><th>${T_('Reported trace','Trace được báo')}</th><th>${T_('Reproduced on the original','Tái hiện trên bản gốc')}</th><th>${T_('After the fix','Sau khi sửa')}</th></tr></thead><tbody>${rows}</tbody></table>`:''}
  ${at.map(x=>`<div class="qt">${T_('Attempt','Lần thử')} ${x.attempt}/${f.max_attempts||3}: <b>${esc((x.commit||{}).name||'')}</b> v${(x.commit||{}).version} ${vChip(FST,x.status)} <span class="muted">${usdE(x.cost_usd)}</span>
   <a class="evl" href="datasets?compare=${x.experiment_id}">${ICN('flask-conical')} ${T_('View experiment','Xem experiment')}</a></div>`).join('')}
  <small class="muted">${T_('Never tagged: production keeps serving the old commit. A verified fix stays "in progress" until a person completes it.','Không gắn tag: production vẫn dùng commit cũ. Cách sửa đã kiểm chứng vẫn ở "đang xử lý" cho tới khi có người hoàn tất.')}</small></div>`}
async function show(id){CUR=id;clearTimeout(POLL);drawList();const d=await api('api/engine/issues/'+id);if(CUR!==id)return;const an=d.analysis,pr=an&&an.proposal,r=d.refs||{};
 const tr={};d.samples.forEach(s=>tr[s.run_id]=s);const sp={};(d.traces||[]).forEach(t=>sp[t.run_id]=t.span_id);const link=s=>`projects?p=${s.project_id}&trace=${s.trace_id}`, elink=s=>link(s)+'&run='+(sp[s.run_id]||s.run_id);
 history.replaceState(null,'','?issue='+id+(QS.get('p')?'&p='+encodeURIComponent(QS.get('p')):''));const ed=canEdit(),dis=ed?'':' disabled';
 const running=(d.retest_results.length&&['pending','running'].includes((d.retest_results.slice(-1)[0].new||{}).status))||(d.validation&&d.validation.baseline&&d.validation.baseline.status==='running')||(d.validation&&d.validation.fix&&d.validation.fix.status==='running');
 let h=`<div class="phead"><h2 style="margin:0">${esc(d.title)}</h2>${stChip(d.status)}${sevChip(d.severity)}<span class="chip">${kindT(d.kind)}</span>${d.category?`<span class="chip">${esc(d.category)}</span>`:''}<span class="sp"></span><span class="muted">${esc(d.project||'')}</span></div>
 ${d.close_reason?`<p class="muted">${T_('Closed as','Đã đóng:')} <b>${T_(...(CLOSE_R[d.close_reason]||[d.close_reason,d.close_reason]))}</b>${d.close_note?' · '+esc(d.close_note):''}</p>`:''}
 ${d.note?`<p class="muted">${esc(d.note)}</p>`:''}
 <div class="kpi" style="margin:12px 0"><div>${T_('Occurrences','Số lần')}<b>${d.count}</b></div><div>${T_('First seen','Lần đầu')}<b style="font-size:13px">${fmtD(d.first_seen)}</b></div><div>${T_('Last seen','Gần nhất')}<b style="font-size:13px">${fmtD(d.last_seen)}</b></div><div>${T_('Analysis cost','Chi phí phân tích')}<b>${usdE(d.cost_usd)}</b></div></div>`;
 if(an){h+=`<div class="cd"><h3>${T_('Root cause','Nguyên nhân gốc')}</h3><div>${esc(an.root_cause)}</div>${(an.evidence||[]).map(e=>e.hidden?`<div class="qt muted">${ICN('lock')} ${T_('private evidence hidden','bằng chứng riêng tư đã ẩn')}</div>`:`<div class="qt">${esc(e.quote)} ${tr[e.run_id]?`<a class="evl" href="${elink(tr[e.run_id])}">${ICN('external-link')} ${T_('trace','trace')}</a>`:''}</div>`).join('')}
  ${an.description?`<p class="muted">${esc(an.description)}</p>`:''}
  ${(an.claims||[]).length?`<h4>${T_('Agent statements checked against the trace','Phát biểu của agent đối chiếu với trace')}</h4>`+an.claims.map(c=>c.hidden?`<div class="qt muted">${ICN('lock')} ${T_('private statement hidden','phát biểu riêng tư đã ẩn')}</div>`:`<div class="qt"><span class="chip">${T_(...({true:['true','đúng'],false:['false','sai'],no_evidence:['no evidence','chưa có bằng chứng']}[c.verdict]||[c.verdict,c.verdict]))}</span> ${esc(c.statement)}${c.quote?` <span class="muted">— ${esc(c.quote)}</span>`:''}</div>`).join(''):''}
  ${(an.sample_notes||[]).some(n=>(n.test_questions||[]).length)?`<h4>${T_('Suggested test questions','Câu hỏi kiểm tra đề xuất')}</h4>`+an.sample_notes.map(n=>`<div class="qt">${tr[n.run_id]?esc(tr[n.run_id].text):''}<ul>${(n.test_questions||[]).map(q=>`<li>${esc(q)}</li>`).join('')}</ul>${n.expected_output?`<div class="muted">${T_('Expected','Mong đợi')}: ${esc(n.expected_output)}</div>`:''}</div>`).join(''):''}
  <small class="muted">${T_('Analysed','Phân tích lúc')} ${fmtD(d.analysed_at)} · ${an.samples_used} ${T_('sample traces','trace mẫu')}</small></div>
  <div class="cd"><h3>${T_('Proposed fix','Đề xuất sửa')} <span class="chip">${esc(pr.type)}</span> <span class="muted" style="font-weight:400">${esc(pr.target)}</span></h3><div>${esc(pr.rationale)}</div>
  ${pr.diff?`<div class="df" style="margin-top:8px">${diffHtml(pr.diff)}</div>`:''}
  ${r.commit?`<p style="margin:8px 0 0">${ICN('git-commit')} ${T_('Applied as Prompt Hub commit','Đã áp dụng thành commit Prompt Hub')} <b>${esc(r.commit.name)}</b> v${r.commit.version} <span class="muted">(${T_('not tagged: production unchanged','chưa gắn tag: production không đổi')})</span></p>`:''}</div>`}
 else h+=`<div class="cd">${d.refs.analysis_error?`<span class="err">${esc(d.refs.analysis_error)}</span><br>`:''}<span class="muted">${T_('No analysis yet.','Chưa có phân tích.')}</span></div>`;
 if(r.dataset_id)h+=`<p class="muted">${ICN('database')} ${T_('Dataset','Dataset')} <b>engine-issue-${d.id}</b> (#${r.dataset_id})</p>`;
 h+=retestHtml(d.retest_results);h+=validationHtml(d.validation);h+=fixHtml(d,tr);
 h+=`<div class="acts"><button id="an" class="g">${ICN('sparkles')}${an?T_('Re-analyse','Phân tích lại'):T_('Analyse','Phân tích')}</button>
  ${pr&&pr.applicable&&!r.commit?`<button id="ap"${BUSY?' disabled aria-busy="true"':''}>${ICN('git-commit')}${T_('Apply (new commit)','Áp dụng (commit mới)')}</button>`:''}
  <button id="ds" class="g">${ICN('database')}${T_('Create dataset','Tạo dataset')}</button>
  ${d.agent_id?`<button id="tv" class="g"${dis} title="${T_('Replay the linked conversations on the current agent version, with every side effect off','Chạy lại các hội thoại liên quan trên phiên bản agent hiện tại, tắt mọi tác dụng phụ')}">${ICN('play')}${T_('Test issue','Chạy thử issue')}</button>`:''}
  ${r.commit?`<button id="rt" class="g">${ICN('flask-conical')}${T_('Re-test old vs new','Chạy lại cũ vs mới')}</button>`:''}
  ${r.commit&&d.agent_id?`<button id="vf" class="g"${dis} title="${T_('Replay the same inputs on the new commit (safe test); if the problem comes back, propose another fix, up to 3 attempts','Chạy lại cùng đầu vào trên commit mới (chạy thử an toàn); nếu vấn đề còn thì đề xuất cách sửa khác, tối đa 3 lần')}">${ICN('check')}${T_('Verify fix','Kiểm chứng cách sửa')}</button>`:''}
  ${an?`<button id="cfx" class="g" title="${T_('Copy this issue as Markdown for a coding assistant','Chép issue này dạng Markdown để đưa cho trợ lý lập trình')}">${ICN('copy')}${T_('Copy for coding assistant','Chép cho trợ lý lập trình')}</button>`:''}
  </div>`;
 const closed=d.status==='completed'||d.status==='ignored';
 h+=`<div class="cd" id="tbx"><h3>${T_('Triage','Phân loại')}</h3>
  <div class="tb"><label>${T_('Priority','Mức ưu tiên')} <select id="pr"${dis}>${SEV.map((x,n)=>`<option value="${n}"${n===d.severity?' selected':''}>${T_(...x)}</option>`).join('')}</select></label>
   <input type="text" id="prr" maxlength="500" placeholder="${T_('Reason for the change (optional)','Lý do đổi (không bắt buộc)')}" value=""${dis}><button id="prs" class="g"${dis}>${T_('Set priority','Đặt mức ưu tiên')}</button></div>
  <div class="tb" style="margin-top:8px">${closed?`<button id="ro" class="g"${dis}>${ICN('rotate-ccw')}${T_('Reopen','Mở lại')}</button>`
   :`<input type="text" id="clr" maxlength="500" placeholder="${T_('Reason / note for closing (optional)','Lý do / ghi chú khi đóng (không bắt buộc)')}"${dis}>
   <button id="cl-res" class="g"${dis}>${ICN('check')}${T_('Close: resolved','Đóng: đã giải quyết')}</button><button id="cl-ni" class="g"${dis}>${T_('Close: not important','Đóng: không quan trọng')}</button><button id="cl-inc" class="g"${dis}>${T_('Incorrectly flagged','Báo nhầm')}</button>${d.status==='watching'?'':`<button id="st-w" class="g"${dis}>${ICN('eye')}${T_('Watch','Theo dõi')}</button>`}`}</div>
  ${ed?'':`<small class="muted">${T_('Read-only: your role cannot change issues.','Chỉ xem: vai trò của bạn không được đổi issue.')}</small>`}</div>
  <div class="cd" id="hist"><h3>${T_('History','Lịch sử')}</h3><ul class="hist" id="hl"><li class="muted">…</li></ul></div>`;
 const sn={};((an&&an.sample_notes)||[]).forEach(n=>sn[n.run_id]=n);
 h+=`<div class="cd"><h3>${T_('Sample traces','Trace mẫu')}</h3>${d.samples.map(s=>{const n=sn[s.run_id]||{};return `<a class="ex" style="display:block;padding:3px 0" href="${link(s)}">${ICN('messages-square')} ${esc(s.text||s.name)} ${s.status==='error'?'<span class="chip er">error</span>':''}</a>
  <details class="oex" data-run="${s.run_id}"><summary class="muted">${ICN('plus')} ${T_('Add offline example','Thêm ví dụ kiểm thử')}</summary>
   <label>${T_('Input','Đầu vào')}<textarea class="oi" rows="2" maxlength="4000"${dis}>${esc(s.text||'')}</textarea></label>
   <label>${T_('Suggested expected output','Đầu ra mong đợi')}<textarea class="oo" rows="3" maxlength="4000"${dis}>${esc(n.expected_output||'')}</textarea></label>
   <label>${T_('Test questions (one per line, 2-5)','Câu hỏi kiểm tra (mỗi dòng một câu, 2-5)')}<textarea class="oq" rows="3"${dis}>${esc((n.test_questions||[]).join('\n'))}</textarea></label>
   <button class="g osv"${dis}>${ICN('database')}${T_('Save to dataset','Lưu vào dataset')}</button></details>`}).join('')}
  ${d.hidden_samples?`<div class="muted">${ICN('lock')} ${T_(d.hidden_samples+' private sample(s) hidden',d.hidden_samples+' mẫu riêng tư đã ẩn')}</div>`:''}</div>`;
 $('iv').innerHTML=h;
 const act=(sel,fn,msg)=>{const b=$(sel);if(b)b.onclick=async()=>{if(BUSY)return;BUSY=true;b.disabled=true;try{const x=await fn();if(msg)flash(msg(x))}catch(e){flash(e.message,1)}BUSY=false;await loadList();show(id)}};
 act('an',()=>api(`api/engine/issues/${id}/analyse`,'POST',{lang:LANG==='vi'?'vi':'en'}),()=>T_('Analysis saved','Đã lưu phân tích'));
 act('ap',()=>api(`api/engine/issues/${id}/apply`,'POST'),x=>T_('Commit created: ','Đã tạo commit: ')+x.name+' v'+x.version);
 act('ds',()=>api(`api/engine/issues/${id}/dataset`,'POST'),x=>T_('Dataset ready: ','Dataset sẵn sàng: ')+x.examples+' '+T_('examples','ví dụ'));
 act('rt',()=>api(`api/engine/issues/${id}/retest`,'POST'),()=>T_('Experiments started','Đã chạy thử nghiệm'));
 act('vf',()=>api(`api/engine/issues/${id}/verify`,'POST',{lang:LANG==='vi'?'vi':'en'}),x=>T_('Verification finished','Đã kiểm chứng xong'));
 document.querySelectorAll('.oex .osv').forEach(b=>b.onclick=async()=>{if(BUSY)return;const bx=b.closest('.oex'),qs=bx.querySelector('.oq').value.split('\n').map(x=>x.trim()).filter(Boolean);BUSY=true;b.disabled=true;try{const x=await api(`api/engine/issues/${id}/examples`,'POST',{items:[{run_id:bx.dataset.run,input:bx.querySelector('.oi').value.trim()||null,expected_output:bx.querySelector('.oo').value.trim()||null,questions:qs}]});flash(T_('Saved to dataset: ','Đã lưu vào dataset: ')+x.results[0])}catch(e){flash(e.message,1)}BUSY=false;b.disabled=false});
 if($('cfx'))$('cfx').onclick=async()=>{try{const r=await fetch(`api/engine/issues/${id}/fix_context`);if(!r.ok)throw new Error('HTTP '+r.status);await navigator.clipboard.writeText(await r.text());flash(T_('Copied','Đã chép'))}catch(e){flash(e.message,1)}};
 act('tv',()=>api(`api/engine/issues/${id}/test`,'POST',{lang:LANG==='vi'?'vi':'en'}),()=>T_('Test finished','Đã chạy thử xong'));
 const pa=(sel,body)=>act(sel,()=>api('api/engine/issues/'+id,'PATCH',typeof body==='function'?body():body),()=>T_('Saved','Đã lưu'));
 const note=()=>($('clr')?$('clr').value.trim():'')||undefined;
 pa('prs',()=>({severity:+$('pr').value,reason:$('prr').value.trim()||undefined}));
 pa('cl-res',()=>({status:'completed',close_reason:'resolved',close_note:note()}));pa('cl-ni',()=>({status:'ignored',close_reason:'not_important',close_note:note()}));
 pa('cl-inc',()=>({status:'ignored',close_reason:'incorrectly_flagged',close_note:note()}));pa('ro',{status:'open'});pa('st-w',{status:'watching'});
 api(`api/engine/issues/${id}/history`).then(x=>{const l=$('hl');if(!l||CUR!==id)return;l.innerHTML=x.items.length?x.items.map(e=>`<li><b>${esc(e.action)}</b> ${e.from_val?esc(e.from_val)+' → ':''}${esc(e.to_val||'')}${e.reason?' · '+esc(e.reason):''} <span class="muted">${esc(e.actor||e.source)} · ${fmtD(e.ts)}</span></li>`).join(''):`<li class="muted">${T_('No changes yet','Chưa có thay đổi')}</li>`}).catch(()=>{});
 if(running)POLL=setTimeout(()=>show(id),3000)}
async function scan(){const b=$('scan');b.disabled=true;try{const r=await api('api/engine/detect?analyse=true','POST');flash(T_(`${r.new.length} new, ${r.reopened.length} reopened, ${r.analysed.length} analysed`,`${r.new.length} mới, ${r.reopened.length} tái diễn, ${r.analysed.length} đã phân tích`));await loadList()}catch(e){flash(e.message,1)}b.disabled=false}
// ---- 12.32 Engine settings per project (panel)
let PJ=[],PSID=null;
const psBody=()=>({enabled:$('ps-on').checked,analysis_level:$('ps-lv').value,scan_every_min:+$('ps-iv').value||60,preferences:$('ps-pf').value,cap_usd_month:$('ps-cap').value===''?null:+$('ps-cap').value,
 auto_validate:$('ps-av').checked,auto_close_not_reproduced:$('ps-ac').checked,replay_safe_tools:$('ps-rt').value.split(',').map(x=>x.trim()).filter(Boolean)});
async function psLoad(pid){PSID=pid;const d=await api(`api/engine/projects/${pid}/settings`),adm=ME.role==='admin'&&!ME.demo,dis=adm?'':' disabled',est=d.estimate;
 $('psb').innerHTML=`${d.env_enabled?'':`<p class="err">${T_('The Engine is switched off on this server (AOS_ENGINE_ENABLED=0).','Engine đang bị tắt trên máy chủ này (AOS_ENGINE_ENABLED=0).')}</p>`}
  <div class="tb"><label><input type="checkbox" id="ps-on"${d.enabled?' checked':''}${dis}> ${T_('Detect problems in this project','Tìm vấn đề trong project này')}</label>
   <span class="chip ${d.active?'ok':''}">${d.active?T_('Active','Đang chạy'):d.paused?T_('Paused','Tạm dừng'):T_('Off','Tắt')}</span>
   ${d.paused?`<button id="ps-rs" class="g"${dis}>${T_('Resume','Tiếp tục')}</button>`:`<button id="ps-pa" class="g"${dis}>${T_('Pause','Tạm dừng')}</button>`}</div>
  <div class="tb" style="margin-top:8px"><label>${T_('Analysis level','Mức phân tích')} <select id="ps-lv"${dis}><option value="reduced"${d.analysis_level==='reduced'?' selected':''}>${T_('Reduced','Rút gọn')}</option><option value="standard"${d.analysis_level==='standard'?' selected':''}>${T_('Standard','Tiêu chuẩn')}</option></select></label>
   <span class="muted" id="ps-est"></span>
   <label>${T_('Scan every (min)','Quét mỗi (phút)')} <input type="number" id="ps-iv" min="10" max="10080" value="${d.scan_every_min}" style="width:80px"${dis}></label></div>
  <div class="tb" style="margin-top:8px"><textarea id="ps-pf" maxlength="2000" rows="2" style="flex:1" placeholder="${T_('Preferences in free text (what matters most for this project)','Ưu tiên bằng lời (điều quan trọng nhất của project này)')}"${dis}>${esc(d.preferences||'')}</textarea></div>
  <div class="tb" style="margin-top:8px"><label><input type="checkbox" id="ps-av"${d.auto_validate?' checked':''}${dis}> ${T_('Test an issue automatically after its first analysis','Tự chạy thử issue sau lần phân tích đầu')}</label>
   <label><input type="checkbox" id="ps-ac"${d.auto_close_not_reproduced?' checked':''}${dis}> ${T_('Close it when the problem cannot be reproduced','Tự đóng khi không tái hiện được')}</label></div>
  <div class="tb" style="margin-top:8px"><input type="text" id="ps-rt" maxlength="800" placeholder="${T_('Extra read-only tools a test may run (comma separated)','Công cụ chỉ-đọc được phép khi chạy thử (cách nhau dấu phẩy)')}" value="${esc((d.replay_safe_tools||[]).join(', '))}"${dis}></div>
  <small class="muted">${T_('A test never sends mail, saves data, changes memory or creates schedules, and rejects every approval.','Chạy thử không bao giờ gửi mail, lưu dữ liệu, đổi bộ nhớ hay tạo lịch, và từ chối mọi yêu cầu duyệt.')}</small>
  <div class="tb" style="margin-top:8px"><label>${T_('Monthly cap (USD, empty = none)','Hạn mức tháng (USD, để trống = không giới hạn)')} <input type="number" id="ps-cap" min="0" step="0.01" value="${d.cap_usd_month==null?'':d.cap_usd_month}" style="width:100px"${dis}></label>
   <span class="muted" id="ps-cost">${T_('Spent this month','Đã dùng tháng này')}: ${usdE(d.month_usd)}${d.cap_usd_month==null?'':' / '+usdE(d.cap_usd_month)}</span></div>
  <div class="muted" style="margin-top:8px">${T_('Last scan','Quét lần cuối')}: ${fmtD(d.last_scan)} · ${T_('Spending cap resets monthly at midnight, Vietnam time (ICT, UTC+7).','Hạn mức chi phí đặt lại mỗi tháng lúc 0 giờ, giờ Việt Nam (ICT, UTC+7).')}</div>
  ${adm?`<div class="tb" style="margin-top:8px"><button id="ps-sv">${T_('Save','Lưu')}</button></div>`:`<small class="muted">${T_('Only a workspace admin can change these settings.','Chỉ quản trị workspace được đổi cài đặt này.')}</small>`}`;
 const e=()=>{const x=est[$('ps-lv').value];$('ps-est').textContent=T_('About','Khoảng')+' '+usdE(x.per_analysis_usd)+' / '+T_('analysis','lần phân tích')+' · '+T_('up to','tối đa')+' '+usdE(x.per_cycle_max_usd)+' / '+T_('scan','lần quét')};e();$('ps-lv').onchange=e;
 const go=async(path,m,body)=>{try{await api(path,body?'PUT':'POST',body);flash(T_('Saved','Đã lưu'));psLoad(pid)}catch(x){flash(x.message,1)}};
 if($('ps-sv'))$('ps-sv').onclick=()=>{if(!$('ps-on').checked&&d.enabled&&!confirm(T_('Turn off problem detection for this project? Existing issues stay readable.','Tắt tìm vấn đề cho project này? Các issue đã có vẫn xem được.')))return;go(`api/engine/projects/${pid}/settings`,0,psBody())};
 if($('ps-pa'))$('ps-pa').onclick=()=>{if(confirm(T_('Pause detection for this project? Existing issues stay readable.','Tạm dừng tìm vấn đề cho project này? Các issue đã có vẫn xem được.')))go(`api/engine/projects/${pid}/pause`)};
 if($('ps-rs'))$('ps-rs').onclick=()=>go(`api/engine/projects/${pid}/resume`)}
async function psOpen(){const p=$('psp');p.hidden=!p.hidden;if(p.hidden)return;PJ=PJ.length?PJ:await api('api/obs/projects');
 const adm=ME.role==='admin'&&!ME.demo,s=await api('api/engine/settings');
 p.innerHTML=`<div class="cd"><h3>${T_('Engine settings','Cài đặt Engine')}</h3><div class="tb"><label><input type="checkbox" id="ps-ws"${s.enabled?' checked':''}${adm?'':' disabled'}> ${T_('Engine on for this workspace','Bật Engine cho workspace này')}</label>
  <select id="ps-pj">${PJ.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select></div><div id="psb" style="margin-top:10px"></div></div>`;
 $('ps-ws').onchange=async e=>{try{await api('api/engine/settings','PUT',{enabled:e.target.checked});flash(T_('Saved','Đã lưu'))}catch(x){flash(x.message,1);e.target.checked=!e.target.checked}};
 const pj=+QS.get('p')||(PJ[0]&&PJ[0].id);if(PJ.some(x=>x.id===pj))$('ps-pj').value=pj;$('ps-pj').onchange=()=>psLoad(+$('ps-pj').value);if(PJ.length)psLoad(+$('ps-pj').value);else $('psb').textContent=T_('No projects yet.','Chưa có project nào.')}
(async()=>{ME=await api('api/auth/me').catch(()=>({}));const s=await api('api/engine/settings').catch(()=>({score_min:0.5}));
 $('hd').innerHTML=`<div class="phead"><span class="ic-tile">${ICN('zap')}</span><h1>Engine</h1><span class="muted">${T_('Recurring problems of your agents: cause, fix, re-test','Vấn đề lặp lại của agent: nguyên nhân, cách sửa, chạy lại')}</span><span class="sp"></span>
  ${ME.demo?'':`<label class="muted" title="${T_('Evaluator scores below this make an issue','Điểm đánh giá thấp hơn mức này sẽ thành issue')}">${T_('Score below','Điểm dưới')} <input id="sm" type="number" step="0.1" min="0" value="${s.score_min}" style="width:64px"${canEdit()?'':' disabled'}></label>`}
  ${ME.demo?'':`<button id="pst" class="g">${ICN('settings')}${T_('Settings','Cài đặt')}</button>`}
  <button id="scan"${canEdit()?'':' disabled'}>${ICN('refresh-cw')}${T_('Scan now','Quét ngay')}</button></div>
  <div class="fl"><select id="fs" title="${T_('Status','Trạng thái')}">${[['open',T_('Open','Đang mở')],['fixing',T_('In progress','Đang xử lý')],['watching',T_('Watching','Theo dõi')],['completed',T_('Fixed','Đã sửa')],['ignored',T_('Ignored','Bỏ qua')],['all',T_('All','Tất cả')]].map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select>
  <select id="fk"><option value="">${T_('All kinds','Mọi loại')}</option>${Object.keys(KIND).map(k=>`<option value="${k}">${kindT(k)}</option>`).join('')}</select>
  <select id="fp" title="${T_('Priority','Mức ưu tiên')}"><option value="">${T_('All priorities','Mọi mức')}</option>${SEV.map((x,n)=>`<option value="${n}">${T_(...x)}</option>`).join('')}</select>
  <select id="fc" title="${T_('Category','Nhóm')}"><option value="">${T_('All categories','Mọi nhóm')}</option></select>
  <select id="so" title="${T_('Sort by','Sắp xếp')}">${[['updated',T_('Sort: Updated','Xếp: Mới cập nhật')],['severity',T_('Sort: Severity','Xếp: Mức ưu tiên')],['traces',T_('Sort: Count','Xếp: Số lần')]].map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></div>`;
 [['fs','open'],['fk',''],['fp',''],['fc',''],['so','updated']].forEach(([k,dv])=>{const e=$(k);const v=VIEW[k]!=null?VIEW[k]:dv;if(k==='fc'&&v){CATS.add(v);e.innerHTML+=`<option value="${esc(v)}">${esc(v)}</option>`}e.value=v;if(e.value!==String(v))e.value=dv});
 $('fs').onchange=$('fk').onchange=$('fp').onchange=$('fc').onchange=$('so').onchange=loadList;$('scan').onclick=scan;if($('pst'))$('pst').onclick=psOpen;
 if($('sm'))$('sm').onchange=async e=>{try{await api('api/engine/settings','PUT',{score_min:+e.target.value});flash(T_('Saved','Đã lưu'))}catch(x){flash(x.message,1)}};
 await loadList();const q=+QS.get('issue')||(LIST[0]&&LIST[0].id);if(q)show(q)})().catch(e=>flash(e.message,1));
"""

ENGINE = page("engine", "Engine", CSS + """<div class="ph" id="hd"></div><div id="psp" hidden style="margin:0 0 12px"></div><div class="eng"><aside id="il"></aside><section id="iv"><div class="muted"></div></section></div>""", JS)
PAGES = {"/engine": ENGINE}
