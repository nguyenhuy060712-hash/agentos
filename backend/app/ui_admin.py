"""UI cho P5 (Agent Server), P6 (Gateway keys), P7 (Quản trị, Sandbox)."""
from ui import page

ADMIN = page("admin", "Quản trị", """<main>
<div class="grid2">
<div class="card"><h2>Người dùng</h2><table id="ul"></table>
<h3>Thêm người dùng</h3><div class="row"><input id="nu" placeholder="tên đăng nhập"><input id="np" type="password" placeholder="mật khẩu ≥10 ký tự"><select id="nr"><option>viewer</option><option>editor</option><option>admin</option></select><button onclick="addU()">Thêm</button></div></div>
<div class="card"><h2>Workspace</h2><table id="wl"></table><div class="row"><input id="nw" placeholder="tên workspace"><button onclick="addW()">Tạo</button></div>
<h3>Phân quyền thành viên</h3><div class="row"><select id="mw"></select><select id="mu"></select><select id="mr"><option>viewer</option><option>editor</option><option>admin</option></select><button onclick="setM()">Gán</button></div>
<small>Vai trò: <b>viewer</b> chỉ xem · <b>editor</b> tạo/sửa/chạy · <b>admin</b> thêm quản lý người dùng, key Gemini, virtual key, redaction, MCP.</small></div>
</div>
<div class="grid2">
<div class="card"><h2>API key của tôi</h2><div class="row"><input id="kn" placeholder="tên key"><select id="kr"><option value="">(theo vai trò)</option><option>viewer</option><option>editor</option></select><select id="ks"><option value="full">Toàn quyền</option><option value="read">Chỉ đọc</option></select><input id="kd" type="number" placeholder="hết hạn (ngày)" style="width:130px"><button onclick="addK()">Tạo key</button></div><div id="knew"></div><table id="kl"></table>
<small>Dùng header <code>X-Api-Key: aos_…</code> cho /api/* và /api/v1 (Agent Server).</small></div>
<div class="card"><h2>Phiên đăng nhập</h2><table id="sl"></table></div>
</div>
<div class="card"><h2>Bí mật</h2><small>Giá trị được mã hoá, không bao giờ hiện lại. Dùng trong header webhook bằng <code>{{secret:TÊN}}</code>; chỉ gửi tới máy trong danh sách cho phép.</small>
<div class="row"><input id="sc_n" placeholder="TÊN_BÍ_MẬT"><input id="sc_v" type="password" autocomplete="new-password" placeholder="giá trị"><input id="sc_h" placeholder="máy được phép (vd api.telegram.org, *.example.com)"><button onclick="addSec()">Thêm</button></div><table id="scl"></table></div>
<div class="card"><h2>Xuất Parquet định kỳ</h2><small>Mỗi ngày lúc 02:00, dữ liệu mới (trace, phản hồi, kết quả đánh giá, chi phí gateway) của workspace được ghi thành file Parquet (nén zstd), mỗi bảng một file mỗi ngày. Chỉ ghi phần mới từ lần xuất trước.</small>
<div class="row"><label><input type="checkbox" id="pq_on"> Bật xuất hằng ngày</label><select id="pq_dest" onchange="pqDest()"><option value="local">Thư mục trên máy chủ</option><option value="s3">Kho S3 (R2, B2, MinIO…)</option></select></div>
<div class="row" id="pq_s3"><input id="pq_ep" placeholder="endpoint (vd https://xxx.r2.cloudflarestorage.com)"><input id="pq_bk" placeholder="bucket"><input id="pq_px" placeholder="thư mục con (tuỳ chọn)"><input id="pq_rg" placeholder="region (tuỳ chọn)"><input id="pq_ak" placeholder="tên bí mật: access key"><input id="pq_sk" placeholder="tên bí mật: secret key"></div>
<small id="pq_hint"></small>
<div class="row"><button onclick="pqSave()">Lưu</button><button class="s" onclick="pqRun(false)">Xuất ngay</button><button class="s r" onclick="pqRun(true)">Xuất lại toàn bộ</button></div>
<div id="pq_last"></div></div>
<div class="card" id="ot_card"><h2>Xuất trace sang OpenTelemetry</h2><small>Gửi các run đã xong của workspace tới bộ thu OTLP bên ngoài (Grafana Tempo, Datadog, Honeycomb, Langfuse…) theo chuẩn GenAI semantic conventions. Mặc định không gửi nội dung hội thoại, chỉ gửi mô hình, token, chi phí, thời gian.</small>
<div class="row"><label><input type="checkbox" id="ot_on"> Bật xuất sang OTLP</label><label><input type="checkbox" id="ot_ct"> Gửi cả nội dung (input/output)</label></div>
<div class="row"><input id="ot_ep" placeholder="địa chỉ OTLP (vd https://otlp.example.com/v1/traces)" style="flex:2"><input id="ot_ev" placeholder="môi trường cần gửi (vd prod,staging; để trống = tất cả)"><input id="ot_ts" type="number" min="10" max="3600" style="width:110px" title="Chu kỳ gửi (giây)"> giây</div>
<div class="row"><textarea id="ot_hd" rows="2" style="flex:1" placeholder="Header, mỗi dòng một cái: Tên: giá trị. Khoá/token phải dùng bí mật: Authorization: Bearer {{secret:TÊN}}"></textarea></div>
<small id="ot_hint"></small>
<div class="row"><button onclick="otSave()">Lưu</button><button class="s" onclick="otRun(false)">Gửi ngay</button><button class="s" onclick="otRun(true)">Gửi lại 24 giờ qua</button></div>
<div id="ot_last"></div></div>
<div class="card" id="cry_card"><h2>Mã hoá dữ liệu khi lưu</h2><small>Bí mật, token OAuth, khoá 2 lớp được mã hoá bằng khoá riêng của từng workspace (AES-256-GCM-SIV); khoá này lại được bọc bằng khoá gốc trong tệp .env của máy chủ. Bật/tắt từng nhóm dữ liệu ngay bên dưới (cần xác nhận). Đổi khoá bằng lệnh <code>python -m aos_crypto rotate</code>.</small>
<div id="cry_body"></div></div>
<div class="card" id="ret_card"><h2>Dọn dữ liệu cũ</h2><small>Chống đầy ổ đĩa. Mặc định TẮT: không tự xoá gì, chỉ dọn sự kiện luồng của lượt chạy cũ (14 ngày). Chỉ động tới workspace này; cuộc trò chuyện, tin nhắn và tệp vẫn giữ nguyên. Xoá không hoàn tác được.</small>
<div class="row"><label><input type="checkbox" id="rt_keep"> Chỉ giữ checkpoint mới nhất của các cuộc trò chuyện đã nhàn rỗi trên 1 giờ</label></div>
<div class="row"><label>Xoá checkpoint của cuộc trò chuyện không hoạt động quá <input type="number" id="rt_ck" min="1" max="3650" style="width:80px" placeholder="tắt"> ngày</label><label>Xoá sự kiện luồng sau <input type="number" id="rt_ev" min="1" max="3650" style="width:80px" placeholder="14"> ngày</label></div>
<div class="row"><button onclick="retSave()">Lưu</button><button class="s" onclick="retPreview()">Xem trước (không xoá)</button><button class="s r" onclick="retRun()">Dọn ngay</button></div>
<pre id="rt_o"></pre></div>
<div class="card" id="pii_card"><h2>Che dữ liệu cá nhân</h2><small>Nhận diện CCCD, điện thoại, mã số thuế, tài khoản ngân hàng, biển số xe, email… (Presidio + bộ nhận diện tiếng Việt). Dữ liệu được che trước khi lưu vào trace và trước khi gửi cho mô hình AI.</small>
<div class="row"><label><input type="checkbox" id="pi_on"> Bật che dữ liệu cá nhân</label><select id="pi_mode" onchange="piHint()"><option value="pseudonym">Tên giả, khôi phục được</option><option value="mask">Che hẳn</option><option value="hash">Băm (mã ổn định)</option></select></div>
<small id="pi_hint"></small>
<div class="row"><label><input type="checkbox" id="pi_tr"> Che trong trace</label><label><input type="checkbox" id="pi_gw"> Che trước khi gửi cho mô hình AI</label><label><input type="checkbox" id="pi_ner"> Dùng mô hình NER (tốn thêm ~300 MB RAM)</label></div>
<div class="row" id="pi_ents" style="flex-wrap:wrap"></div>
<div class="row"><button onclick="piSave()">Lưu</button></div>
<h3>Thử che nhanh</h3><div class="row"><textarea id="pi_t" rows="2" style="flex:1" placeholder="Dán một đoạn có số điện thoại, CCCD…"></textarea><button class="s" onclick="piTest()">Thử che</button></div><pre id="pi_o"></pre>
<h3>Quyền của chủ dữ liệu</h3><small>Xuất hoặc xoá dữ liệu của một người dùng theo yêu cầu của họ (chỉ trong workspace này). Xoá dữ liệu không hoàn tác được.</small>
<div class="row"><select id="pi_u"></select><button class="s" onclick="piExport()">Xuất dữ liệu (zip)</button><button class="s r" onclick="piErase()">Xoá dữ liệu theo yêu cầu</button></div>
<table id="pi_req"></table></div>
<div class="card" id="gr_card"><h2>Nhóm người dùng</h2><small>Dùng để chia sẻ agent cho nhiều người cùng lúc (trang agent, nút Chia sẻ). Mọi thành viên của nhóm có cùng quyền.</small>
<table id="gr_l"></table>
<h3>Tạo nhóm</h3><div class="row"><input id="gr_n" placeholder="tên nhóm (vd Kinh doanh)"><input id="gr_d" placeholder="mô tả (tuỳ chọn)" style="flex:2"><button onclick="grAdd()">Tạo nhóm</button></div></div>
<div class="card"><h2>Audit log</h2><div class="row"><input id="af" placeholder="lọc theo user"><input id="ap" placeholder="lọc theo đường dẫn (prefix)"><button class="s" onclick="audit()">Lọc</button></div><table id="al"></table></div>
</main>""", r"""
let U=[],W=[];
async function load(){try{U=await api('api/admin/users');W=await api('api/admin/workspaces')}catch(e){document.querySelector('main').innerHTML='<div class="card">'+esc(e.message)+'</div>';return}
 $('ul').innerHTML='<tr><th>#</th><th>Tên</th><th>Quyền</th><th>Đăng nhập cuối</th><th></th></tr>'+U.map(u=>`<tr><td>${u.id}</td><td><b>${esc(u.username)}</b>${u.is_superadmin?' <svg class="ic"><use href="#i-star"/></svg>':''}${u.disabled?' <span class="tag" style="background:#fee2e2">khoá</span>':''}</td>
 <td>${(u.memberships||[]).map(m=>'ws'+m.workspace_id+':'+m.role).join(', ')}</td><td>${vn(u.last_login)}</td>
 <td>${u.is_superadmin?'':`<button class="s" onclick="tog(${u.id},${!u.disabled})">${u.disabled?'Mở khoá':'Khoá'}</button> <button class="s r" onclick="delU(${u.id})">Xoá</button>`}</td></tr>`).join('');
 $('wl').innerHTML='<tr><th>#</th><th>Tên</th><th>Thành viên</th><th>Agents</th><th>Projects</th></tr>'+W.map(w=>`<tr><td>${w.id}</td><td>${esc(w.name)}</td><td>${w.members}</td><td>${w.agents}</td><td>${w.projects}</td></tr>`).join('');
 $('mw').innerHTML=W.map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('');$('mu').innerHTML=U.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');
 const ks=await api('api/auth/keys');$('kl').innerHTML='<tr><th>Tên</th><th>Prefix</th><th>Vai trò</th><th>Phạm vi</th><th>Dùng cuối</th><th></th></tr>'+ks.map(k=>`<tr><td>${esc(k.name)}</td><td><code>${esc(k.prefix)}…</code></td><td>${k.role||'(vai trò)'}</td><td>${k.scope==='read'?'Chỉ đọc':'Toàn quyền'}</td><td>${vn(k.last_used)}</td><td>${k.revoked?'đã thu hồi':`<button class="s r" onclick="revK(${k.id})">Thu hồi</button>`}</td></tr>`).join('');
 const ss=await api('api/admin/sessions');$('sl').innerHTML='<tr><th>User</th><th>IP</th><th>Tạo</th><th>Hết hạn</th></tr>'+ss.map(s=>`<tr><td>${esc(s.username)}</td><td>${esc(s.ip)}</td><td>${vn(s.created)}</td><td>${vn(s.expires)}</td></tr>`).join('');
 audit();grLoad()}
let G=[];
async function grLoad(){const me=await api('api/auth/me');G=await api('api/admin/groups');const inWs=U.filter(u=>(u.memberships||[]).some(m=>m.workspace_id===me.ws));
 $('gr_l').innerHTML='<tr><th>Nhóm</th><th>Thành viên</th><th>Agent đã chia sẻ</th><th></th></tr>'+(G.length?G.map(g=>`<tr><td><b>${esc(g.name)}</b><br><small>${esc(g.description||'')}</small></td>
  <td>${g.members.map(m=>`<span class="tag">${esc(m.username)} <a href="#" title="Bỏ khỏi nhóm" onclick="grSet(${g.id},${m.id},false);return false">✕</a></span>`).join(' ')||'—'}
  <div class="row"><select id="gm${g.id}">${inWs.filter(u=>!g.members.some(m=>m.id===u.id)).map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('')}</select><button class="s" onclick="grSet(${g.id},+$('gm${g.id}').value,true)">Thêm vào nhóm</button></div></td>
  <td>${g.shared_agents}</td><td><button class="s r" onclick="grRemove(${g.id})">Xoá nhóm</button></td></tr>`).join(''):'<tr><td colspan="4">Chưa có nhóm nào.</td></tr>')}
async function grAdd(){try{await api('api/admin/groups','POST',{name:$('gr_n').value,description:$('gr_d').value});$('gr_n').value='';$('gr_d').value='';grLoad();flash('Đã tạo nhóm')}catch(e){flash(e.message,1)}}
async function grSet(gid,uid,add){const g=G.find(x=>x.id===gid);if(!uid)return;const ids=g.members.map(m=>m.id).filter(i=>i!==uid);if(add)ids.push(uid);
 try{await api('api/admin/groups/'+gid,'PATCH',{members:ids});grLoad()}catch(e){flash(e.message,1)}}
async function grRemove(gid){if(!confirm('Xoá nhóm? Các lượt chia sẻ agent cho nhóm này cũng bị gỡ.'))return;try{await api('api/admin/groups/'+gid,'DELETE');grLoad()}catch(e){flash(e.message,1)}}
async function audit(){const p=new URLSearchParams();if($('af').value)p.set('user',$('af').value);if($('ap').value)p.set('path',$('ap').value);
 const a=await api('api/admin/audit?'+p);$('al').innerHTML='<tr><th>Lúc</th><th>User</th><th>Xác thực</th><th>Thao tác</th><th>KQ</th><th>IP</th><th>Ghi chú</th></tr>'+a.map(x=>`<tr><td>${vn(x.ts)}</td><td>${esc(x.username)}</td><td><small>${esc(x.auth)}</small></td><td><code>${x.method} ${esc(x.path)}</code></td><td>${x.status}</td><td>${esc(x.ip)}</td><td>${esc(x.detail||'')}</td></tr>`).join('')}
async function addU(){try{await api('api/admin/users','POST',{username:$('nu').value,password:$('np').value,role:$('nr').value});$('np').value='';load();flash('Đã thêm')}catch(e){flash(e.message,1)}}
async function tog(id,d){try{await api('api/admin/users/'+id,'PATCH',{disabled:d});load()}catch(e){flash(e.message,1)}}
async function delU(id){if(!confirm('Xoá người dùng?'))return;try{await api('api/admin/users/'+id,'DELETE');load()}catch(e){flash(e.message,1)}}
async function addW(){try{await api('api/admin/workspaces','POST',{name:$('nw').value});load()}catch(e){flash(e.message,1)}}
async function setM(){try{await api('api/admin/workspaces/'+$('mw').value+'/members','POST',{user_id:+$('mu').value,role:$('mr').value});load()}catch(e){flash(e.message,1)}}
async function addK(){try{const r=await api('api/auth/keys','POST',{name:$('kn').value||'key',role:$('kr').value||null,scope:$('ks').value,expires_days:$('kd').value?+$('kd').value:null});
 $('knew').innerHTML=`<p>Key (chỉ hiện một lần): <input style="width:70%" readonly value="${esc(r.key)}"></p>`;load()}catch(e){flash(e.message,1)}}
async function revK(id){await api('api/auth/keys/'+id,'DELETE');load()}
async function loadSec(){try{const r=await api('api/secrets');$('scl').innerHTML='<tr><th>Tên</th><th>Máy được phép</th><th>Cập nhật</th><th></th></tr>'+r.map(x=>`<tr><td><code>${esc(x.name)}</code></td><td>${esc((x.allowed_hosts||[]).join(', '))}</td><td>${vn(x.updated_at)}</td><td><button class="s r" onclick="delSec('${esc(x.name)}')">Xoá</button></td></tr>`).join('')}catch(e){$('scl').innerHTML=''}}
async function addSec(){try{await api('api/secrets/'+encodeURIComponent($('sc_n').value),'POST',{value:$('sc_v').value,allowed_hosts:$('sc_h').value.split(',').map(x=>x.trim()).filter(Boolean)});$('sc_v').value='';loadSec();flash('Đã lưu')}catch(e){flash(e.message,1)}}
async function delSec(n){if(!confirm('Xoá bí mật?'))return;try{await api('api/secrets/'+encodeURIComponent(n),'DELETE');loadSec()}catch(e){flash(e.message,1)}}

const PQT={traces:'Trace (run + span)',feedback:'Phản hồi',eval_results:'Kết quả đánh giá',gateway_usage:'Chi phí gateway'};
function pqDest(){$('pq_s3').style.display=$('pq_dest').value==='s3'?'':'none'}
function pqShow(c){$('pq_on').checked=!!c.enabled;$('pq_dest').value=c.dest||'local';$('pq_ep').value=c.s3_endpoint||'';$('pq_bk').value=c.s3_bucket||'';$('pq_px').value=c.s3_prefix||'';$('pq_rg').value=c.s3_region||'';$('pq_ak').value=c.access_key_secret||'';$('pq_sk').value=c.secret_key_secret||'';pqDest();
 $('pq_hint').textContent=c.local_dir?('Thư mục: '+c.local_dir):'Khoá S3 lấy từ mục Bí mật ở trên; máy được phép của 2 bí mật phải gồm host của endpoint.';
 let h='';if(c.last_run_at){const t=c.last_counts||{};h='<p>Lần xuất cuối: '+vn(c.last_run_at)+' · '+(c.last_status==='ok'?'thành công':'lỗi')+' ('+(c.last_mode==='full'?'toàn bộ':c.last_mode==='scheduled'?'theo lịch':'thủ công')+')'+(c.last_error?' · '+esc(c.last_error):'')+'</p><table><tr><th>Bảng</th><th>Dòng mới</th><th>Tổng đã xuất</th></tr>'+Object.keys(PQT).map(k=>'<tr><td>'+PQT[k]+' (<code>'+k+'</code>)</td><td>'+((t[k]||{}).added??'-')+'</td><td>'+((c.totals||{})[k]??0)+'</td></tr>').join('')+'</table>'}else h='<p>Chưa xuất lần nào.</p>';
 $('pq_last').innerHTML=h}
async function pqLoad(){try{pqShow(await api('api/exports/parquet'))}catch(e){}}
async function pqSave(){try{pqShow(await api('api/exports/parquet','PUT',{enabled:$('pq_on').checked,dest:$('pq_dest').value,s3_endpoint:$('pq_ep').value||null,s3_bucket:$('pq_bk').value||null,s3_prefix:$('pq_px').value,s3_region:$('pq_rg').value||null,access_key_secret:$('pq_ak').value||null,secret_key_secret:$('pq_sk').value||null}));flash('Đã lưu')}catch(e){flash(e.message,1)}}
async function pqRun(full){if(full&&!confirm('Xoá các file cũ của workspace này và xuất lại từ đầu?'))return;try{pqShow(await api('api/exports/parquet/run','POST',{full}));flash('Đã xuất xong')}catch(e){flash(e.message,1);pqLoad()}}

function otHeaders(){const o={};$('ot_hd').value.split('\n').forEach(l=>{const i=l.indexOf(':');if(i>0)o[l.slice(0,i).trim()]=l.slice(i+1).trim()});return o}
function otShow(c){$('ot_on').checked=!!c.enabled;$('ot_ct').checked=!!c.include_content;$('ot_ep').value=c.endpoint||'';$('ot_ev').value=(c.environments||[]).join(',');$('ot_ts').value=c.every_s||60;
 $('ot_hd').value=Object.entries(c.headers||{}).map(([k,v])=>k+': '+v).join('\n');$('ot_hint').textContent='Chuẩn: OTel GenAI semconv '+(c.semconv_version||'')+'. Bí mật phải có sẵn ở mục Bí mật phía trên và cho phép host của endpoint.';
 let h='';if(c.last_run_at){h='<p>Lần gửi cuối: '+vn(c.last_run_at)+' · '+(c.last_status==='ok'?'thành công':'lỗi')+' ('+(c.last_count||0)+' span) · tổng '+(c.total||0)+(c.last_error?' · <span class="err">'+esc(c.last_error)+'</span>':'')+'</p>'}
 $('ot_last').innerHTML=h}
async function otLoad(){try{otShow(await api('api/exports/otlp'))}catch(e){}}
async function otSave(){try{otShow(await api('api/exports/otlp','PUT',{enabled:$('ot_on').checked,endpoint:$('ot_ep').value,headers:otHeaders(),include_content:$('ot_ct').checked,environments:$('ot_ev').value.split(',').map(x=>x.trim()).filter(Boolean),every_s:+$('ot_ts').value||60}));flash('Đã lưu')}catch(e){flash(e.message,1)}}
async function otRun(back){try{otShow(await api('api/exports/otlp/run','POST',{from_start:back}));flash('Đã gửi')}catch(e){flash(e.message,1);otLoad()}}

let RT=null;
async function retLoad(){try{RT=await api('api/admin/retention')}catch(e){$('ret_card').style.display='none';return}
 $('rt_keep').checked=RT.checkpoint_keep_latest;$('rt_ck').value=RT.checkpoint_days||'';$('rt_ev').value=RT.run_events_days||'';$('rt_ev').placeholder=String(Math.round(RT.defaults.run_events_days));
 $('rt_o').textContent=RT.last_run_at?'Lần dọn gần nhất: '+vn(RT.last_run_at)+' '+JSON.stringify(RT.last_counts||{}):''}
async function retSave(){try{await api('api/admin/retention','PUT',{checkpoint_keep_latest:$('rt_keep').checked,checkpoint_days:$('rt_ck').value?+$('rt_ck').value:null,run_events_days:$('rt_ev').value?+$('rt_ev').value:null});flash('Đã lưu');retLoad()}catch(e){flash(e.message,1)}}
async function retPreview(){try{const r=await api('api/admin/retention/preview','POST',{});$('rt_o').textContent='Sẽ xoá checkpoint của '+r.threads_to_clear+' cuộc trò chuyện, rút gọn '+r.threads_to_prune+' cuộc, xoá '+r.run_events_to_delete+' sự kiện luồng. Chưa xoá gì.'}catch(e){flash(e.message,1)}}
async function retRun(){if(!confirm('Dọn dữ liệu cũ ngay theo cài đặt trên? Không hoàn tác được.'))return;try{const r=await api('api/admin/retention/run','POST',{confirm:true});flash('Đã dọn');retLoad();$('rt_o').textContent='Đã xoá checkpoint của '+r.cleared+' cuộc trò chuyện, rút gọn '+r.pruned+' cuộc, xoá '+r.run_events+' sự kiện luồng.'}catch(e){flash(e.message,1)}}
let PI=null;
function piHint(){const m=$('pi_mode').value;$('pi_hint').textContent=m==='pseudonym'?'Mô hình AI chỉ thấy tên giả như <PHONE_1>; câu trả lời được đổi lại thành dữ liệu thật cho người dùng. Trace lưu bản đã che.':m==='hash'?'Giá trị được thay bằng mã băm ổn định, không khôi phục được.':'Giá trị được thay bằng nhãn như [PHONE], không khôi phục được.'}
async function piLoad(){try{PI=await api('api/privacy/settings')}catch(e){return}
 $('pi_on').checked=PI.enabled;$('pi_mode').value=PI.mode;$('pi_tr').checked=PI.apply_traces;$('pi_gw').checked=PI.apply_gateway;$('pi_ner').checked=PI.ner;$('pi_ner').disabled=!PI.ner_available;
 $('pi_ents').innerHTML=PI.catalogue.map(e=>`<label style="margin-right:12px"><input type="checkbox" data-e="${e.id}" ${PI.entities.includes(e.id)?'checked':''}> ${esc(e.label_vi)}</label>`).join('');
 document.querySelectorAll('#pii_card input,#pii_card select').forEach(x=>{if(x.id!=='pi_u')x.disabled=x.disabled||!PI.can_edit});if(!PI.can_edit)document.querySelector('#pii_card .row button').disabled=true;
 $('pi_u').innerHTML=(U||[]).filter(u=>!u.is_superadmin).map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');piHint();piReq()}
async function piSave(){try{await api('api/privacy/settings','PUT',{enabled:$('pi_on').checked,mode:$('pi_mode').value,apply_traces:$('pi_tr').checked,apply_gateway:$('pi_gw').checked,ner:$('pi_ner').checked,
 entities:[...document.querySelectorAll('#pi_ents input:checked')].map(x=>x.dataset.e),threshold:0.5});flash('Đã lưu');piLoad()}catch(e){flash(e.message,1)}}
async function piTest(){try{const r=await api('api/privacy/test','POST',{text:$('pi_t').value,mode:$('pi_mode').value,entities:[...document.querySelectorAll('#pi_ents input:checked')].map(x=>x.dataset.e)});
 $('pi_o').textContent=r.masked+(r.findings.length?'\n\n'+r.findings.map(f=>f.tag+' ('+f.score+'): '+f.text).join('\n'):'')}catch(e){flash(e.message,1)}}
function piExport(){const id=$('pi_u').value;if(id)location.href='api/admin/privacy/users/'+id+'/export'}
async function piErase(){const o=$('pi_u').selectedOptions[0];if(!o)return;const n=prompt('Gõ tên đăng nhập "'+o.textContent+'" để xác nhận xoá dữ liệu:');if(n===null)return;
 try{const r=await api('api/admin/privacy/users/'+o.value+'/erase','POST',{confirm:n});flash('Đã xoá dữ liệu');piReq();load()}catch(e){flash(e.message,1)}}
async function piReq(){try{const r=await api('api/privacy/requests');$('pi_req').innerHTML='<tr><th>Thời gian</th><th>Loại</th><th>Người dùng</th><th>Do ai</th><th>Số lượng</th></tr>'+r.map(x=>`<tr><td>${vn(x.created)}</td><td>${x.kind==='erase'?'Xoá':'Xuất'}</td><td>${esc(x.subject_label||x.subject_user_id)}</td><td>${x.origin==='self'?'Chính họ':'Admin'} (${esc(x.requested_by_name||'')})</td><td><small>${esc(Object.entries(x.counts||{}).filter(([k,v])=>typeof v==='number').map(([k,v])=>k+': '+v).join(', '))}</small></td></tr>`).join('')}catch(e){}}
const CRY_ON=v=>v?'<b style="color:#0a7d33">Đang bật</b>':'Tắt';
const CRY_G=[['checkpoints','Tin nhắn trong lịch sử chat (checkpoint)'],['traces','Nội dung trace vào/ra'],['runs','Lượt chạy và sự kiện luồng (runs)'],['datasets','Ví dụ trong bộ dữ liệu (đầu vào/đầu ra)'],['attachments','Tệp đính kèm chat (nội dung tệp + văn bản trích xuất)']];
async function cryToggle(g,el){const on=el.checked,lb=(CRY_G.find(x=>x[0]===g)||[])[1]||g;
 if(!confirm(on?'Bật mã hoá cho: '+lb+'?\nDữ liệu cũ sẽ được mã hoá lại ở nền (có thể mất vài phút). Từ giờ dữ liệu mới của workspace này được lưu dạng mã hoá. Tìm kiếm văn bản trực tiếp trong SQL sẽ không dùng được với nhóm này.':'Tắt mã hoá cho: '+lb+'?\nDữ liệu đã mã hoá vẫn đọc được bình thường; chỉ dữ liệu mới từ giờ được lưu dạng rõ (không mã hoá).')){el.checked=!on;return}
 el.disabled=true;try{await api('api/crypto/settings','PUT',{[g]:on});flash(on?'Đã bật mã hoá — đang mã hoá dữ liệu cũ ở nền':'Đã tắt mã hoá cho nhóm này')}catch(e){el.checked=!on;flash(e.message,1)}cryLoad()}
async function cryLoad(){try{const c=await api('api/crypto/status');const w=c.workspace;let h='<p>Khoá gốc đang dùng: <code>'+esc(c.active_kid)+'</code> · Thuật toán: <code>'+esc(c.alg_default)+'</code> · Khoá dữ liệu chưa bọc lại bằng khoá gốc mới: <b>'+c.kek_stale_deks+'</b> · Dòng cũ chưa chuyển sang định dạng mới: <b>'+c.legacy_rows+'</b></p>';
 h+='<p><b>Workspace này — chọn nhóm dữ liệu cần mã hoá</b></p><div id="cry_tog">'+CRY_G.map(([g,l])=>'<label style="display:flex;gap:8px;align-items:center;margin:4px 0"><input type="checkbox" id="cry_'+g+'" '+(w[g]?'checked ':'')+'data-g="'+g+'" onchange="cryToggle(this.dataset.g,this)"> '+l+' — '+CRY_ON(w[g])+'</label>').join('')+'</div>'+(c.migrate?'<p>Mã hoá dữ liệu cũ: <b>'+esc(c.migrate.state)+'</b></p>':'')+'<p>Khoá dữ liệu đang dùng: '+(w.deks.filter(d=>d.status==='active').map(d=>'#'+d.id+' ('+esc(d.alg)+')').join(', ')||'chưa tạo')+'</p>';
 h+='<table><tr><th>Cột dữ liệu</th><th>Số dòng theo phiên bản / khoá</th></tr>'+Object.keys(c.columns).map(k=>'<tr><td><code>'+esc(k)+'</code></td><td>'+(Object.entries(c.columns[k]).map(([a,b])=>esc(a)+': '+b).join(' · ')||'-')+'</td></tr>').join('')+'<tr><td>Checkpoint chat</td><td>'+(Object.entries(c.checkpoints).map(([a,b])=>esc(a)+': '+b).join(' · ')||'-')+'</td></tr><tr><td>Trace (vào/ra)</td><td>'+(Object.entries(c.traces).map(([a,b])=>esc(a)+': '+b).join(' · ')||'-')+'</td></tr>'+Object.entries(c.payloads||{}).map(([k,v])=>'<tr><td><code>'+esc(k)+'</code></td><td>'+(Object.entries(v).map(([a,b])=>esc(a)+': '+b).join(' · ')||'-')+'</td></tr>').join('')+'</table>';
 h+='<small>Khi bật mã hoá trace, không tìm kiếm văn bản trực tiếp trong nội dung trace bằng SQL được; xem trước rút gọn hiện "[đã mã hoá]".</small>';$('cry_body').innerHTML=h}catch(e){$('cry_body').textContent=e.message}}
load().then(piLoad);retLoad();loadSec();pqLoad();otLoad();cryLoad();""")

SANDBOXES = page("sandboxes", "Sandbox", """<main>
<div class="grid2" style="grid-template-columns:320px 1fr">
<div class="card"><h2>Sandbox bền</h2><small>Mỗi sandbox có thư mục riêng (≤50MB), chạy Python trong container không mạng, read-only, 256MB RAM. File giữ lại giữa các lần chạy; tự xoá khi quá hạn không dùng.</small>
<div class="row"><input id="sn" placeholder="tên"><input id="st" type="number" value="168" style="width:80px" title="TTL giờ"><button onclick="addS()">Tạo</button></div><div id="sl"></div></div>
<div class="card"><h2 id="sh">Chọn sandbox</h2><div id="sw" style="display:none">
<textarea id="code" rows="8">import os, json
open('ghi_chu.txt','a').write('chạy lúc đầu\\n')
print(os.listdir('.'))</textarea><div class="row"><button onclick="run()">▶ Chạy</button><span id="rs"></span></div><pre id="out"></pre>
<h3>File</h3><div class="row"><input type="file" id="up"><button class="s" onclick="upl()">Tải lên</button></div><table id="fl"></table></div></div></div></main>""", r"""
let CUR=null;
async function load(){const s=await api('api/sandboxes');$('sl').innerHTML=s.map(x=>`<div class="card" style="padding:8px;margin:6px 0;cursor:pointer;${x.id==CUR?'border-color:#2563eb':''}" onclick="pick('${x.id}')"><b>${esc(x.name)}</b> <code>${x.id}</code><br><small>${x.runs} lần chạy · ${(x.bytes/1024).toFixed(1)} KB · TTL ${x.ttl_hours}h · ${esc(x.owner||'')}</small> <button class="s r" onclick="event.stopPropagation();delS('${x.id}')">Xoá</button></div>`).join('')||'<small>Chưa có</small>'}
async function addS(){const r=await api('api/sandboxes','POST',{name:$('sn').value||'sandbox',ttl_hours:+$('st').value});CUR=r.id;load();pick(r.id)}
async function delS(id){if(!confirm('Xoá sandbox và toàn bộ file?'))return;await api('api/sandboxes/'+id,'DELETE');if(CUR==id){CUR=null;$('sw').style.display='none'}load()}
async function pick(id){CUR=id;$('sh').textContent='Sandbox '+id;$('sw').style.display='';files();load()}
async function files(){const f=await api('api/sandboxes/'+CUR+'/files');$('fl').innerHTML='<tr><th>File</th><th>Kích thước</th><th></th></tr>'+f.map(x=>`<tr><td>${esc(x.path)}</td><td>${x.bytes}</td><td><a href="api/sandboxes/${CUR}/files/${encodeURIComponent(x.path)}">Tải về</a> <button class="s r" onclick="rmF('${esc(x.path)}')">Xoá</button></td></tr>`).join('')}
async function run(){$('rs').textContent='đang chạy…';try{const r=await api('api/sandboxes/'+CUR+'/exec','POST',{code:$('code').value});$('rs').textContent=`exit=${r.exit} · ${r.ms}ms`+(r.warning?' · '+r.warning:'');$('out').textContent=r.stdout+(r.stderr?'\n[stderr]\n'+r.stderr:'');files();load()}catch(e){$('rs').textContent=e.message}}
async function upl(){const fd=new FormData();fd.append('file',$('up').files[0]);try{await api('api/sandboxes/'+CUR+'/files','POST',fd);files()}catch(e){flash(e.message,1)}}
async function rmF(p){await api('api/sandboxes/'+CUR+'/files/'+encodeURIComponent(p),'DELETE');files()}
load();""")

GWKEYS = page("gwkeys", "Gateway keys", """<main>
<div class="card"><h2>Virtual key — endpoint tương thích OpenAI</h2>
<p style="font-size:13px">Base URL: <code id="bu"></code> · <code>POST /v1/chat/completions</code> (stream, tools, JSON mode, ảnh base64), <code>GET /v1/models</code>, <code>POST /v1/embeddings</code>. Chỉ model Gemini. Mỗi key: model cho phép, fallback, ngân sách USD/tháng, RPM, cache (TTL), guardrail PII (mask/block cả vào và ra).</p>
<div class="row"><input id="n" placeholder="tên key"><input id="m" placeholder="model cho phép (phẩy; trống = tất cả)" style="width:260px"><input id="b" type="number" step="0.01" placeholder="USD/tháng" style="width:110px">
<input id="r" type="number" placeholder="RPM" style="width:80px"><select id="p"><option value="mask">PII: mask</option><option value="block">PII: block</option><option value="off">PII: tắt</option></select>
<input id="c" type="number" placeholder="cache TTL (s)" style="width:120px"><button onclick="add()">Tạo key</button></div><div id="new"></div><table id="kl"></table></div>
<div class="card"><h3>Thử nhanh</h3><div class="row"><input id="tk" placeholder="vk-… (key vừa tạo)" style="width:340px"><input id="tm" value="gemini-flash-lite-latest"><input id="tq" style="flex:1" value="Email tôi là an@vidu.vn, chào bạn bằng một câu."><button onclick="test()">Gửi</button></div><pre id="to"></pre></div>
</main>""", r"""
$('bu').textContent=location.origin+'/v1';
async function load(){const k=await api('api/gw/keys');$('kl').innerHTML='<tr><th>Tên</th><th>Prefix</th><th>Model</th><th>Ngân sách</th><th>Đã tiêu (tháng)</th><th>RPM</th><th>PII</th><th>Cache</th><th>24h gọi / cache hit</th><th></th></tr>'+k.map(x=>`<tr><td>${esc(x.name)}</td><td><code>${x.prefix}…</code></td><td>${esc((x.models||['tất cả']).join(', '))}</td><td>${x.budget_usd_month??'–'}</td><td>${usd(x.spent_month)}</td><td>${x.rpm??'–'}</td><td>${x.pii_mode}</td><td>${x.cache_ttl_s?x.cache_ttl_s+'s':'tắt'}</td><td>${x.calls_24h} / ${x.cache_hits_24h}</td>
 <td><button class="s" onclick="tog(${x.id},${!x.enabled})">${x.enabled?'Tắt':'Bật'}</button> <button class="s r" onclick="del(${x.id})">Xoá</button></td></tr>`).join('')}
async function add(){try{const r=await api('api/gw/keys','POST',{name:$('n').value||'key',models:$('m').value?$('m').value.split(',').map(s=>s.trim()):null,budget_usd_month:$('b').value?+$('b').value:null,rpm:$('r').value?+$('r').value:null,pii_mode:$('p').value,cache_ttl_s:+($('c').value||0)});
 $('new').innerHTML=`<p>Key (chỉ hiện một lần): <input style="width:60%" readonly value="${esc(r.key)}"></p>`;$('tk').value=r.key;load()}catch(e){flash(e.message,1)}}
async function tog(id,en){await api('api/gw/keys/'+id,'PATCH',{enabled:en});load()}
async function del(id){if(confirm('Xoá key?')){await api('api/gw/keys/'+id,'DELETE');load()}}
async function test(){$('to').textContent='…';const r=await fetch('v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+$('tk').value},body:JSON.stringify({model:$('tm').value,messages:[{role:'user',content:$('tq').value}]})});
 $('to').textContent='HTTP '+r.status+'\n'+JSON.stringify(await r.json(),null,1);load()}
load();""")

SERVER = page("server", "Agent Server", """<main>
<div class="card"><h2>Agent Server API <small id="qi"></small></h2><p style="font-size:13px">REST tại <code>/api/v1</code>: assistants · threads · runs (nền qua hàng đợi Postgres, stream SSE, wait, join, cancel) · stateless runs · cron · webhook · double-texting (reject/enqueue/interrupt/rollback). Xác thực: phiên đăng nhập hoặc <code>X-Api-Key: aos_…</code>. Tài liệu đầy đủ: <a href="docs#/agent-server">/docs</a>.</p></div>
<div class="grid2">
<div class="card"><h3>Assistants</h3><table id="al"></table></div>
<div class="card"><h3>Chạy thử (stream SSE)</h3><div class="row"><select id="sa"></select><input id="st" placeholder="thread_id (trống = tạo mới)"><select id="ms"><option>enqueue</option><option>reject</option><option>interrupt</option><option>rollback</option></select></div>
<div class="row"><input id="sq" style="flex:1" value="Bây giờ là mấy giờ?"><button onclick="go()">Stream</button><button class="g" onclick="bg()">Chạy nền</button></div><pre id="so" style="max-height:300px;overflow:auto"></pre></div>
</div>
<div class="grid2"><div class="card"><h3>Threads gần đây <select id="own" style="display:none;font-size:12px" onchange="OWN=this.value;load()" title="Của ai"></select></h3><table id="tl"></table></div><div class="card"><h3>Runs của thread <span id="rt"></span></h3><table id="rl"></table></div></div>
<div class="card"><h3>Cron</h3><div class="row"><input id="cs" value="0 7 * * *" style="width:120px"><input id="cq" style="flex:1" placeholder="tin nhắn"><button class="s" onclick="addC()">Thêm cron cho assistant đang chọn</button></div><table id="cl"></table></div>
</main>""", r"""
let AS=[],OWN='';
async function owners(){try{const m=await api('api/auth/me');if(m.role!=='admin')return;const us=await api('api/admin/users');$('own').innerHTML='<option value="">Của ai: tất cả</option>'+us.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');$('own').style.display=''}catch(e){}}
async function load(){const i=await api('api/v1/info');$('qi').textContent=`· hàng đợi: ${JSON.stringify(i.queue)} · worker ${i.workers.active}/${i.workers.max}`;
 AS=await api('api/v1/assistants/search','POST',{limit:100});$('al').innerHTML='<tr><th>Tên</th><th>graph</th><th>v</th><th>id</th></tr>'+AS.map(a=>`<tr><td>${esc(a.name)}</td><td>${a.graph_id}</td><td>${a.version}</td><td><small>${a.assistant_id}</small></td></tr>`).join('');
 $('sa').innerHTML=AS.map(a=>`<option value="${a.assistant_id}">${esc(a.name)}</option>`).join('');
 const t=await api('api/v1/threads/search','POST',{limit:15,owner_id:OWN||undefined});$('tl').innerHTML='<tr><th>Thread</th><th>Của ai</th><th>Trạng thái</th><th>Cập nhật</th></tr>'+t.map(x=>`<tr style="cursor:pointer" onclick="runs('${esc(x.thread_id)}')"><td><small>${esc(x.thread_id)}</small></td><td>${esc(x.owner||'')}</td><td>${x.status}</td><td>${vn(x.updated_at)}</td></tr>`).join('');
 const c=await api('api/v1/runs/crons/search','POST',{limit:50});$('cl').innerHTML='<tr><th>Lịch</th><th>Assistant</th><th>Bật</th><th>Lần cuối</th><th></th></tr>'+c.map(x=>`<tr><td><code>${x.schedule}</code></td><td><small>${x.assistant_id}</small></td><td>${x.enabled}</td><td>${vn(x.last_run)}</td><td><button class="s" onclick="cTog('${x.cron_id}',${!x.enabled})">${x.enabled?'Tắt':'Bật'}</button> <button class="s r" onclick="cDel('${x.cron_id}')">Xoá</button></td></tr>`).join('')}
async function runs(t){$('rt').textContent=t;const r=await api('api/v1/threads/'+encodeURIComponent(t)+'/runs?limit=20');$('rl').innerHTML='<tr><th>Run</th><th>Trạng thái</th><th>Chiến lược</th><th>Tạo</th><th>Trace</th></tr>'+r.map(x=>`<tr><td><small>${x.run_id.slice(0,8)}</small></td><td>${x.status}</td><td>${x.multitask_strategy}</td><td>${vn(x.created_at)}</td><td>${x.trace_id?`<a href="projects?p=1&trace=${x.trace_id}">xem</a>`:''}</td></tr>`).join('')}
async function ensureT(){let t=$('st').value;if(!t){const r=await api('api/v1/threads','POST',{});t=r.thread_id;$('st').value=t}return t}
async function go(){const t=await ensureT();$('so').textContent='';const r=await fetch('api/v1/threads/'+encodeURIComponent(t)+'/runs/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({assistant_id:$('sa').value,input:{messages:[{role:'user',content:$('sq').value}]},stream_mode:['updates','values'],multitask_strategy:$('ms').value})});
 if(!r.ok){$('so').textContent='HTTP '+r.status+' '+await r.text();return}const rd=r.body.getReader(),dec=new TextDecoder();while(true){const {value,done}=await rd.read();if(done)break;$('so').textContent+=dec.decode(value);$('so').scrollTop=1e9}runs(t);load()}
async function bg(){const t=await ensureT();try{const r=await api('api/v1/threads/'+encodeURIComponent(t)+'/runs','POST',{assistant_id:$('sa').value,input:{messages:[{role:'user',content:$('sq').value}]},multitask_strategy:$('ms').value});flash('run '+r.run_id.slice(0,8)+' '+r.status);runs(t)}catch(e){flash(e.message,1)}}
async function addC(){try{await api('api/v1/runs/crons','POST',{assistant_id:$('sa').value,schedule:$('cs').value,input:{messages:[{role:'user',content:$('cq').value||'Báo cáo nhanh'}]}});load()}catch(e){flash(e.message,1)}}
async function cTog(id,en){await api('api/v1/runs/crons/'+id,'PATCH',{enabled:en});load()}
async function cDel(id){await api('api/v1/runs/crons/'+id,'DELETE');load()}
owners();load();""")

PAGES = {"/admin": ADMIN, "/sandboxes": SANDBOXES, "/gwkeys": GWKEYS, "/server": SERVER}
