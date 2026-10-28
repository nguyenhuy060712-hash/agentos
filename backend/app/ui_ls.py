"""Trang mới theo spec LangSmith: Overview (All agents / Agent Overview)."""
from ui import page

COMMON_JS = r"""
const fmtN=v=>v==null?'–':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(1)+'K':String(v);
const fmtS=ms=>ms==null?'–':(ms/1000).toFixed(2)+'s';
const latPill=ms=>ms==null?'<span class="muted">–</span>':`<span class="chip pill lat ${ms<5000?'ok':ms<10000?'wn':'er'}">${ICN('clock')}${fmtS(ms)}</span>`;
const errPill=r=>`<span class="chip pill ${r>0.05?'er':'ok'}">${(r*100).toFixed(r&&r<0.01?1:0)}%</span>`;
const rel=t=>{if(!t)return'–';const s=(Date.now()-new Date(t))/1000;return s<60?Math.round(s)+'s ago':s<3600?Math.round(s/60)+'m ago':s<86400?Math.round(s/3600)+'h ago':Math.round(s/86400)+'d ago'};
function spark(pts,w=220,h=44){if(!pts.length)return'';const mx=Math.max(...pts,1);const st=w/Math.max(pts.length-1,1);
 const p=pts.map((v,i)=>`${(i*st).toFixed(1)},${(h-2-(v/mx)*(h-6)).toFixed(1)}`).join(' ');
 return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polygon points="0,${h} ${p} ${w},${h}" fill="var(--sel)"/><polyline points="${p}" fill="none" stroke="var(--brand)" stroke-width="1.5"/></svg>`}
function idChip(v){return `<span class="chip id" title="Copy ID" onclick="navigator.clipboard.writeText('${esc(v)}');flash('Copied')">ID: ${esc(v)} ${ICN('copy')}</span>`}
"""

OVERVIEW = page("overview", "Overview", """<style>
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}.acard{border:1px solid var(--bd2);border-radius:var(--r-lg);padding:14px;cursor:pointer;background:var(--s1);display:flex;flex-direction:column;gap:8px;text-decoration:none;color:inherit}
.acard:hover{border-color:var(--brand);text-decoration:none;box-shadow:var(--sh-sm)}.acard .st{display:flex;gap:14px;font-size:12px;color:var(--t3)}.acard .st b{color:var(--t1);font-weight:600}
.acard.new{border-style:dashed;align-items:flex-start;justify-content:center;color:var(--t2)}
.mrow{display:grid;grid-template-columns:1.6fr repeat(4,1fr);border:1px solid var(--bd2);border-radius:var(--r-lg);overflow:hidden}.mrow>div{padding:12px 16px;border-right:1px solid var(--bd2)}.mrow>div:last-child{border:0}
.mrow .lbl{font-size:12px;color:var(--t3)}.mrow .big{font-size:22px;font-weight:600}
.tl{position:relative;padding-left:40px;display:grid;gap:22px}.tl:before{content:'';position:absolute;left:13px;top:6px;bottom:6px;width:1px;background:var(--bd2)}
.tl .sec{position:relative}.tl .sec>.tile{position:absolute;left:-40px;top:-2px;width:28px;height:28px;background:var(--sel);color:var(--brand)}
.tl .sec p{margin:2px 0 10px;color:var(--t3);font-size:13px}.promo{background:#0f172a;color:#e2e8f0;border-radius:var(--r-lg);padding:14px 16px;font-size:13px}
</style><main id="ov"></main>""", COMMON_JS + r"""
const box=$('ov');
async function allAgents(){const a=await api('api/ui/agents_summary?days=7');
 if(!a.length||new URLSearchParams(location.search).get('welcome')){box.innerHTML=`<div style="max-width:880px;margin:6vh auto;text-align:center"><h1 style="font-size:22px">Do you already have an agent?</h1><div class="muted" style="margin:6px 0 22px">Connect an agent you run elsewhere, or build a new one here.</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;text-align:left"><a class="acard" href="#" id="wyes" style="padding:20px"><span class="tile" style="background:var(--sel);color:var(--brand)">${ICN('plug')}</span><b style="font-size:15px">Yes, I have one</b><div class="muted">Trace an existing agent. Send runs from your code with an API key and see them here.</div></a>
  <a class="acard" href="builder?new=1" style="padding:20px"><span class="tile" style="background:var(--sel);color:var(--brand)">${ICN('sparkles')}</span><b style="font-size:15px">No, I'm starting fresh</b><div class="muted">Describe what the agent should do and build it without code, or start from a template.</div></a></div></div>`;
  $('wyes').onclick=e=>{e.preventDefault();(window.aosConnectAgent||(()=>location.href='builder'))()};return}
 box.innerHTML=`<div class="phead"><h1>All agents</h1><span class="sp"></span><a class="btn" href="builder">${ICN('plus')}Agent</a></div>
 <div class="muted" style="margin-top:-8px">Showing data from the last 7 days</div>
 <div class="agrid">${a.map(x=>`<a class="acard" href="overview?agent=${x.id}"><div class="row" style="flex-wrap:nowrap">${agTile(x)}<b style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.name)}</b><span class="chip">v${x.current_version}</span></div>
  <div class="mono muted" style="font-size:11.5px">${esc(x.identifier||'')}</div>
  <div class="st"><span><b>${fmtN(x.traces)}</b> traces</span><span><b>${x.traces?((x.errors/x.traces)*100).toFixed(0):0}%</b> errors</span><span><b>${fmtN(x.tokens)}</b> tokens</span><span><b>$${(+x.cost).toFixed(4)}</b></span></div>
  <div class="muted" style="font-size:12px">Last run ${rel(x.last_run)}</div></a>`).join('')}
  <div class="acard new"><b>New Agent</b><a class="btn" href="builder">${ICN('plus')}Create new agent</a><a class="btn g" href="projects">${ICN('activity')}Trace existing agent</a></div></div>`}
async function one(id){const o=await api('api/ui/agents/'+id+'/overview?days=7');const m=o.metrics;
 const envRows=o.environments.map(e=>`<tr class="r" onclick="location.href='projects?agent=${id}&env=${e.environment}'"><td><b style="text-transform:capitalize">${e.environment}</b><div class="mono muted" style="font-size:11px">${esc(e.project)}</div></td>
  <td>${rel(e.last)}</td><td>${fmtN(e.traces)}</td><td>${errPill(e.traces?e.errors/e.traces:0)}</td><td>${latPill(e.p50_ms)}</td><td>${latPill(e.p99_ms)}</td><td class="muted">${esc(e.description)}</td></tr>`).join('');
 box.innerHTML=`<div class="phead">${agTile(o.agent)}<h1>${esc(o.agent.name)}</h1>${idChip(o.agent.identifier)}<a class="gh btn g" href="agents?id=${id}" title="Settings">${ICN('settings')}</a><span class="sp"></span>
  <a class="btn g" href="./?agent=${id}">${ICN('message-circle')}Chat</a><a class="btn g" href="studio?agent=${id}">${ICN('workflow')}Studio</a></div>
 <div class="muted" style="margin-top:-8px">Showing data from the last 7 days across all environments</div>
 <div class="mrow"><div><div class="lbl">${esc(o.agent.name)} · traces</div>${spark(o.sparkline.map(x=>x.n))}</div>
  <div><div class="lbl">Trace count</div><div class="big">${fmtN(m.traces)}</div></div><div><div class="lbl">Error rate</div><div class="big">${(m.error_rate*100).toFixed(1)}%</div></div>
  <div><div class="lbl">Token count</div><div class="big">${fmtN(m.tokens)}</div></div><div><div class="lbl">Total cost</div><div class="big">$${(+m.cost).toFixed(4)}</div></div></div>
 <div class="tl">
  <div class="sec"><span class="tile">${ICN('activity')}</span><div class="h4">Tracing</div><p>Every environment this agent traces to, with its run volume, error rate and latency.</p>
   <div class="card" style="padding:0"><table><thead><tr><th>Environment</th><th>Most recent run</th><th>Trace count</th><th>Error rate</th><th>P50 latency</th><th>P99 latency</th><th>Description</th></tr></thead><tbody>${envRows}</tbody></table></div></div>
  <div class="sec"><span class="tile">${ICN('zap')}</span><div class="h4">Engine</div><p>Engine found ${o.error_traces} error traces across this agent's environments. <a href="projects?agent=${id}&tab=engine">Go to Engine →</a></p></div>
  <div class="sec"><span class="tile">${ICN('gauge')}</span><div class="h4">Evaluate your agent</div><p>Run experiments on datasets and attach online evaluators to environments.</p><a class="btn g" href="evaluators">Go to evaluators</a></div>
  <div class="sec"><span class="tile">${ICN('chart-line')}</span><div class="h4">Monitor your agent</div><p>Prebuilt dashboards per environment, custom charts and alerts.</p><a class="btn g" href="monitoring?agent=${id}">Go to dashboards</a></div>
  <div class="sec"><span class="tile">${ICN('rocket')}</span><div class="h4">Deployments</div><p>Deployments bound to this agent.</p>
   ${o.deployments.length?`<div class="card" style="padding:0"><table><thead><tr><th>Name</th><th>Status</th><th>Environment</th><th>Version</th><th>Updated</th></tr></thead><tbody>${o.deployments.map(d=>`<tr class="r" onclick="location.href='deployments'"><td class="mono">${esc(d.slug)}</td><td><span class="chip pill ${d.status==='active'?'ok':''}"><span class="dot ${d.status==='active'?'':'y'}"></span>${d.status==='active'?'Ready':esc(d.status)}</span></td><td style="text-transform:capitalize">${esc(d.environment)}</td><td>v${d.version}</td><td>${rel(d.updated)}</td></tr>`).join('')}</tbody></table></div>`:`<a class="btn g" href="deployments">${ICN('plus')}New Deployment</a>`}</div>
 </div>`}
(async()=>{try{const id=new URLSearchParams(location.search).get('agent')||localStorage.getItem('aos_agent');if(id)await one(id);else await allAgents()}catch(e){box.innerHTML=`<div class="empty"><span class="tile">${ICN('circle-alert')}</span><b>Could not load</b>${esc(e.message)}</div>`}})();
""")

PAGES = {"/overview": OVERVIEW}
