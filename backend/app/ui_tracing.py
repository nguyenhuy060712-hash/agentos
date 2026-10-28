"""Tracing page in the LangSmith layout (spec §3, §4): project header + tabs, filter bar with chips / saved views /
time range / Threads|Traces|Runs, LangSmith column set, floating bulk-action bar, and the trace slide-over
(Trajectory / Turns / Details; Feedback / Input / Output / Attributes). Self-written; the existing filter DSL is the back end."""
from ui import page
from ui_ls import COMMON_JS

CSS = """<style>
.thead{padding:14px 20px 0;border-bottom:1px solid var(--bd2)}.thead .phead h1{font-size:17px}.thead .tabs{border:0;margin:10px 0 0}
.tbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 20px;border-bottom:1px solid var(--bd3)}
.fchip{display:inline-flex;align-items:center;gap:4px;height:26px;padding:0 4px 0 8px;border:1px solid var(--bd2);border-radius:var(--r-md);background:var(--s2);font-size:12px}
.fchip b{font-weight:500}.fchip i{font-style:normal;color:var(--t3)}.fchip button{height:20px;padding:0 3px}
.twrap{display:flex;min-height:0}.twrap .tbl{flex:1;overflow:auto}.fs{width:240px;border-left:1px solid var(--bd2);padding:10px 12px;font-size:12px;overflow:auto;display:none}.fs.on{display:block}
.fs .grp{margin-bottom:10px}.fs .grp b{display:block;color:var(--t3);font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.04em;margin-bottom:4px}
.fs a{display:flex;justify-content:space-between;padding:3px 6px;border-radius:var(--r-sm);color:var(--t1);cursor:pointer}.fs a:hover{background:var(--s3);text-decoration:none}
.rt{width:20px;height:20px;border-radius:var(--r-sm);display:inline-flex;align-items:center;justify-content:center;flex:none;color:#fff}.rt .ic{width:13px;height:13px}
.rt.llm{background:#ee7207}.rt.chain{background:#006ddd}.rt.tool{background:#0e9384}.rt.retriever{background:#7a5af8}.rt.prompt{background:#ca8504}.rt.parser,.rt.other{background:#64748b}
.nm{display:flex;align-items:center;gap:8px;font-weight:500;white-space:nowrap}.trunc{max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t2)}
td{vertical-align:middle}.tbl td{white-space:nowrap;padding:6px 8px;height:38px}.st-ok{color:var(--dot-g)}.st-er{color:var(--dot-r)}
.pane .ptop{height:44px;display:flex;align-items:center;gap:4px;padding:0 10px;border-bottom:1px solid var(--bd2);flex:none}
.pane .pbody{flex:1;display:flex;min-height:0}.pane.full{width:100vw}
.tree{width:40%;min-width:300px;border-right:1px solid var(--bd2);display:flex;flex-direction:column;min-height:0}.tree .th{display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--bd3)}
.tree .sum{padding:6px 10px;font-size:12px;color:var(--t3);border-bottom:1px solid var(--bd3);display:flex;gap:10px}.tree .rows{overflow:auto;flex:1;padding:4px 0}
.tn{padding:5px 10px;cursor:pointer;border-left:2px solid transparent}.tn:hover{background:var(--s2)}.tn.on{background:var(--sel);border-left-color:var(--brand)}
.tn .l1{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:500}.tn .l2{display:flex;gap:4px;margin-top:3px;flex-wrap:wrap}
.wf{position:relative;height:6px;background:var(--s3);border-radius:3px;margin-top:4px}.wf div{position:absolute;height:6px;border-radius:3px;background:var(--brand)}
.det{flex:1;overflow:auto;padding:12px 16px;min-width:0}.det .sec{margin-top:12px}.det .sec>summary{font-weight:600;font-size:13px;cursor:pointer;list-style:none;display:flex;gap:6px;align-items:center}
.bub{display:flex;gap:8px;margin:6px 0;border:1px solid var(--bd2);border-radius:var(--r-lg);padding:8px 10px}.bub .who{width:22px;height:22px;border-radius:var(--r-sm);background:var(--s4);display:inline-flex;align-items:center;justify-content:center;flex:none;font-size:10px;font-weight:600}
.bub .tx{white-space:pre-wrap;font-size:13px;line-height:1.5;min-width:0;overflow-wrap:anywhere}.bub .rl{font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase}
.kv{display:grid;grid-template-columns:minmax(120px,32%) 1fr;font-size:12.5px}.kv>div{padding:5px 6px;border-bottom:1px solid var(--bd3);overflow-wrap:anywhere}.kv .k{font-family:var(--mono);color:var(--t3)}
.kv .v{position:relative}.kv .v .act{position:absolute;right:2px;top:2px;display:none;gap:2px}.kv .v:hover .act{display:flex}
.traj{max-width:860px;margin:0 auto;padding:16px}.traj .hu{margin-left:auto;max-width:75%;background:var(--s3);padding:8px 12px;border-radius:12px;white-space:pre-wrap;width:fit-content}
.traj .ai{margin:10px 0;white-space:pre-wrap;line-height:1.55}.traj .tc{border:1px solid var(--bd2);border-radius:var(--r-lg);padding:6px 10px;margin:6px 0;font-size:12.5px;display:flex;gap:8px;align-items:center;cursor:pointer}
.traj .meta{font-size:11.5px;color:var(--t3);display:flex;gap:8px;margin-top:4px}
.turn{border:1px solid var(--bd2);border-radius:var(--r-lg);margin:10px 16px;padding:10px 12px}
.sbar{display:flex;justify-content:flex-end;padding:4px 10px;border-bottom:1px solid var(--bd3);flex:none}.psearch{display:flex;align-items:center;gap:2px}.psearch input{width:260px;height:28px}.psearch .sc{font-size:11.5px;color:var(--t3);min-width:44px;text-align:center;white-space:nowrap}
#pview button[data-n]::after{content:attr(data-n);margin-left:5px;font-size:10.5px;padding:0 5px;border-radius:8px;background:var(--brand);color:#fff}
mark{background:#fde68a;color:inherit;border-radius:2px;padding:0 1px}[data-k].hit{outline:1px solid #f5c542;outline-offset:-1px}[data-k].hit.cur{outline:2px solid var(--brand);background:var(--sel)}
.tl{max-width:900px;margin:0 auto;padding:16px 16px 24px}.st{display:flex;gap:10px;position:relative;padding-bottom:10px;cursor:pointer}
.st::before{content:"";position:absolute;left:11px;top:24px;bottom:-2px;width:2px;background:var(--bd2)}.st:last-of-type::before{display:none}
.st .dot{width:24px;height:24px;border-radius:50%;flex:none;display:inline-flex;align-items:center;justify-content:center;color:#fff;z-index:1;background:#64748b}.st .dot .ic{width:13px;height:13px}
.st .dot.k-model{background:#ee7207}.st .dot.k-tool{background:#0e9384}.st .dot.k-agent{background:#7a5af8}.st .dot.k-run{background:#006ddd}.st .dot.k-approval{background:#16a34a}.st .dot.k-interrupt{background:#ca8504}.st .dot.k-error{background:var(--dot-r)}
.st .sb{flex:1;min-width:0;border:1px solid var(--bd2);border-radius:var(--r-lg);padding:6px 10px}.st:hover .sb{background:var(--s2)}
.st .l1{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:13px}.st .sub,.st .res{font-size:12px;color:var(--t2);margin-top:3px;overflow-wrap:anywhere}.st .res{color:var(--t1)}
.mw{max-width:820px;margin:0 auto;padding:16px}.mb{display:flex;gap:8px;margin:8px 0}.mb .bx{border:1px solid var(--bd2);border-radius:14px;padding:8px 12px;min-width:0;max-width:85%}
.mb.human{flex-direction:row-reverse}.mb.human .bx{background:var(--s3)}.mb.system .bx{border-style:dashed}.mb.tool .bx{background:var(--s2);max-width:85%}
.mb .rl{font-size:11px;color:var(--t3);font-weight:600;text-transform:uppercase}.mb .tx{white-space:pre-wrap;font-size:13px;line-height:1.5;overflow-wrap:anywhere}
.mb details>summary{cursor:pointer;font-size:12.5px;list-style:none;display:flex;gap:6px;align-items:center}.mb details .tx{margin-top:6px}
.th2{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:18px 0 6px;padding-bottom:6px;border-bottom:1px solid var(--bd3);font-size:12.5px}.th2:first-child{margin-top:0}
.chip.ext{background:var(--s3);color:var(--t2);font-size:10.5px;height:18px;padding:0 6px;margin-left:6px;text-transform:none;font-weight:500}.chip.ext .ic{width:11px;height:11px}
.thumb{max-width:220px;max-height:160px;border-radius:8px;border:1px solid var(--bd2);margin:6px 6px 0 0;display:block}.imgs{display:flex;flex-wrap:wrap;gap:4px}
.sa{margin:8px 0;border-left:2px solid var(--bd2);padding-left:10px}.sa>summary{cursor:pointer;list-style:none;display:flex;gap:6px;align-items:center;font-size:12.5px;padding:2px 0}.sa .sab{margin-left:4px}
.pg{margin:8px 0;border:1px dashed var(--bd2);border-radius:12px;padding:6px 8px}.pgl{display:flex;gap:6px;align-items:center;font-size:11.5px;color:var(--t3);margin-bottom:2px}.pgr{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 8px}.pgr .mb .bx{max-width:100%;width:100%}.pgr .mb details>summary{flex-wrap:wrap}
.doc{margin-top:6px;padding:6px 8px;border:1px solid var(--bd3);border-radius:8px;background:var(--s1)}.doc .tx{margin-top:2px;color:var(--t2)}
.tnote{display:flex;gap:6px;align-items:center;margin:0 0 10px;padding:6px 10px;border-radius:8px;background:var(--s3);font-size:12px;color:var(--t2)}
.mat{margin-top:16px;border-top:1px solid var(--bd3);padding-top:10px}.att{margin:6px 0;font-size:12.5px}.att summary{cursor:pointer;display:flex;gap:6px;align-items:center}.attp{max-height:240px;overflow:auto;white-space:pre-wrap;font-size:12px;margin:6px 0 0}
.env{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:10px;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.02em;border:1px solid transparent;background:#eef2f6;color:#475467}
.env-prod{background:#fdecea;color:#b42318;border-color:#f7c6c1}.env-staging{background:#fef4e0;color:#93370d;border-color:#f6dca8}.env-dev{background:#e6f4ea;color:#067647;border-color:#b7e2c4}
.xp{margin-top:8px;padding:8px 10px;border:1px dashed var(--bd2);border-radius:10px;font-size:12.5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.xp code{font-size:11.5px;background:var(--s2);padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}
</style>"""

BODY = CSS + """<div id="landing" style="display:none"><main>
<div class="phead"><span class="tile" style="background:var(--sel);color:var(--brand)"><svg class="ic"><use href="#i-activity"/></svg></span><h1>Tracing Projects</h1><span class="sp"></span>
<select id="penv" title="Environment"><option value="">All environments</option><option value="dev">dev</option><option value="staging">staging</option><option value="prod">prod</option></select>
<input id="np" placeholder="New project name" style="width:200px"><select id="npe" title="Environment of the new project"><option value="dev">dev</option><option value="staging">staging</option><option value="prod">prod</option></select><button id="npb"><svg class="ic"><use href="#i-plus"/></svg>New Project</button></div>
<div class="card" style="padding:0"><table><thead><tr><th>Name</th><th>Environment</th><th>Trace count (24h)</th><th>Error rate (24h)</th><th>Total traces</th><th>Retention</th><th>Most recent run</th></tr></thead><tbody id="pl"></tbody></table></div></main></div>
<div id="proj" style="display:none;flex-direction:column;height:100%">
<div class="thead"><div class="phead"><span class="tile" style="background:var(--sel);color:var(--brand)"><svg class="ic"><use href="#i-activity"/></svg></span><h1 id="pt"></h1><span id="pid"></span>
<select id="psel" title="Switch project" style="height:28px"></select><span class="sp"></span>
<button class="g" id="pset" title="Project settings"><svg class="ic"><use href="#i-settings"/></svg></button><span class="chip" id="pret" title="Retention"></span>
<a class="btn g" id="pdash"><svg class="ic"><use href="#i-chart-line"/></svg>Dashboard</a><a class="btn g" href="deployments"><svg class="ic"><use href="#i-rocket"/></svg>Deployment</a>
<a class="btn g" href="rules" title="Alerts"><svg class="ic"><use href="#i-bell"/></svg></a><button id="pnew" data-menu="1"><svg class="ic"><use href="#i-plus"/></svg>New</button></div>
<div class="tabs" id="ptabs"><button data-t="tracing" class="on">Tracing</button><button data-t="evaluators">Evaluators</button><button data-t="automations">Automations</button><button data-t="insights">Insights</button><button data-t="engine">Engine <span class="chip pill ok" style="height:16px;font-size:10px">New</span></button></div></div>
<div id="tab_tracing" style="display:flex;flex-direction:column;flex:1;min-height:0">
<div class="tbar"><button class="g" id="vbtn" data-menu="1"><svg class="ic"><use href="#i-bookmark"/></svg><span id="vname">Default View</span><svg class="ic"><use href="#i-chevron-down"/></svg></button>
<button class="g" id="tbtn" data-menu="1"><svg class="ic"><use href="#i-hourglass"/></svg><span id="tname">Last 1 day</span></button>
<span class="seg" id="mode"><button data-m="threads">Threads</button><button data-m="traces" class="on">Traces</button><button data-m="runs">Runs</button></span>
<select id="own" style="display:none" title="Của ai"></select><span id="chips" class="row" style="gap:4px"></span><button class="g" id="addf" data-menu="1"><svg class="ic"><use href="#i-filter"/></svg>Add filter</button>
<span class="sp"></span><input id="fts" placeholder="Search input and output… or status:error -tag:x latency:[2 TO 10]" style="width:220px">
<button class="gh" id="cpf" title="Copy filter"><svg class="ic"><use href="#i-copy"/></svg></button><button class="gh" id="rawb" title="Raw query"><svg class="ic"><use href="#i-code"/></svg></button><button class="g" id="nlb" data-ro-ok="1"><svg class="ic"><use href="#i-sparkles"/></svg><span id="nlbt"></span></button>
<button class="g" id="saveas">Save as</button><button class="g" id="savev" style="display:none">Save</button>
<button class="gh" id="fsb" title="Filter Shortcuts"><svg class="ic"><use href="#i-panel-right"/></svg></button></div>
<div id="nl" style="display:none;padding:8px 20px;border-bottom:1px solid var(--bd3)"><div class="row"><input id="nlq" style="flex:1" maxlength="500"><button class="g" id="nlask" data-ro-ok="1"></button><button id="nlrun" data-ro-ok="1" style="display:none"></button></div><div id="nlex" class="muted" style="font-size:12px;margin-top:4px"></div><div id="nlerr" class="err" style="font-size:12px"></div></div>
<div id="raw" style="display:none;padding:8px 20px;border-bottom:1px solid var(--bd3)"><div class="row"><input id="rawq" class="mono" style="flex:1" placeholder='and(eq(status,"error"), gt(latency,2))'><button class="g" id="rawgo">Apply</button></div><div id="ferr" class="err" style="font-size:12px"></div></div>
<div class="twrap" style="flex:1"><div class="tbl"><table><thead id="rh"></thead><tbody id="rb"></tbody></table><div id="more" style="padding:10px 20px"></div></div><aside class="fs" id="fs"></aside></div></div>
<div id="tab_other" style="display:none;padding:16px 20px;overflow:auto"></div></div>
<div class="bulk" id="bulk"><b id="bn"></b><button class="g" id="baq" data-menu="1"><svg class="ic"><use href="#i-square-pen"/></svg>Add to Annotation Queue</button><button id="bds" data-menu="1"><svg class="ic"><use href="#i-database"/></svg>Add to Dataset</button><button class="gh" id="bx"><svg class="ic"><use href="#i-x"/></svg></button></div>
<div class="pane" id="pane"><div class="ptop"><button class="gh" id="pclose" title="Close"><svg class="ic"><use href="#i-chevrons-right"/></svg></button><button class="gh" id="pfull" title="Expand"><svg class="ic"><use href="#i-maximize"/></svg></button>
<button class="gh" id="pprev" title="Previous"><svg class="ic"><use href="#i-arrow-up"/></svg></button><button class="gh" id="pnext" title="Next"><svg class="ic"><use href="#i-arrow-down"/></svg></button><button class="gh" id="prf" title="Refresh"><svg class="ic"><use href="#i-refresh-cw"/></svg></button>
<span id="ptitle" class="row" style="gap:6px;font-weight:600;font-size:13px"></span><span class="sp"></span>
<span class="seg" id="pview"><button data-v="trajectory">Trajectory</button><button data-v="messages">Messages</button><button data-v="turns">Turns</button><button data-v="details" class="on">Details</button></span>
<button class="gh" id="paq" title="Add to Annotation Queue" data-menu="1"><svg class="ic"><use href="#i-square-pen"/></svg></button><a class="btn" id="pstudio"><svg class="ic"><use href="#i-play"/></svg>Run in Studio</a></div>
<div class="sbar"><span class="psearch"><input id="sq" placeholder="Search in trace… (Ctrl+F)" autocomplete="off"><span class="sc" id="scount"></span><button class="gh" id="sprev" title="Previous match"><svg class="ic"><use href="#i-arrow-up"/></svg></button><button class="gh" id="snext" title="Next match"><svg class="ic"><use href="#i-arrow-down"/></svg></button></span></div><div class="pbody" id="pbody"></div></div>"""

JS = COMMON_JS + r"""
const RT_ICON={llm:'sparkles',chain:'waypoints',tool:'wrench',retriever:'library',prompt:'file-text',parser:'box'};
const rtTile=t=>`<span class="rt ${RT_ICON[t]?t:'other'}" title="${esc(t)}">${ICN(RT_ICON[t]||'box')}</span>`;
const stIcon=s=>s==='error'?`<span class="st-er" title="Error">${ICN('circle-alert')}</span>`:s==='running'||s==='pending'?`<span class="muted">${ICN('loader')}</span>`:`<span class="st-ok" title="Success">${ICN('circle-check')}</span>`;
const tokCost=(t,c)=>(t||c)?`<span class="chip" title="Tokens / Cost">${ICN('coins')}${fmtN(t||0)}${c?' / $'+Number(c).toFixed(4):''}</span>`:'';
const dt=t=>t?new Date(t).toLocaleString(LANG==='vi'?'vi-VN':'en-US',{timeZone:'Asia/Bangkok',month:'numeric',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'}):'';
const preview=v=>{if(v==null)return'';let s=typeof v==='string'?v:JSON.stringify(v);try{const o=typeof v==='string'?JSON.parse(v):v;s=o.message||o.question||o.input||o.answer||o.output||o.text||(o.messages&&JSON.stringify(o.messages.slice(-1)))||s}catch(e){}return String(s)};
const QS=new URLSearchParams(location.search);
let PROJ=null,PROJS=[],MODE='traces',CHIPS=[],RAW='',SINCE=24,ROWS=[],OWN='',VIEW=null,VIEWS=[],SELIDS=new Set(),QUEUES=[],DSETS=[],PTAB='tracing';
const TIMES=[[1,'Last 1 hour'],[24,'Last 1 day'],[168,'Last 7 days'],[720,'Last 30 days'],[0,'All time']];
// ---- filter chips → DSL
const ATTRS=[['name','Run name','s'],['run_type','Run type','s'],['status','Status','s'],['tag','Tag','s'],['model','Model','s'],['thread_id','Thread ID','s'],
 ['environment','Environment','s'],['latency','Latency (s)','n'],['total_tokens','Tokens','n'],['cost','Cost','n'],['feedback','Feedback','fb'],['metadata','Metadata','md'],['search','Full-text search','fts']];
const OPS_S=[['is','eq'],['is not','neq'],['contains','like'],['does not contain','nlike'],['is one of','in']],OPS_N=[['>','gt'],['<','lt'],['is','eq']];
const q_=v=>JSON.stringify(String(v));
function chipDSL(c){const f=c.attr==='feedback'?'feedback.'+c.key:c.attr==='metadata'?'metadata.'+c.key:c.attr;
 if(c.attr==='search')return `search(${q_(c.value)})`;
 if(c.attr==='tag')return c.op==='neq'||c.op==='nlike'?`not(has(tags,${q_(c.value)}))`:`has(tags,${q_(c.value)})`;
 const num=['latency','total_tokens','cost','feedback'].includes(c.attr),val=num?Number(c.value):q_(c.value);
 if(c.op==='like')return `like(${f},${q_('%'+c.value+'%')})`;if(c.op==='nlike')return `not(like(${f},${q_('%'+c.value+'%')}))`;
 if(c.op==='in')return `in(${f},${String(c.value).split(',').map(s=>q_(s.trim())).join(',')})`;return `${c.op}(${f},${val})`}
const isQ=s=>/(?:^|\s)-?[A-Za-z_][\w.\-]*:\S/.test(s),qtext=()=>{const s=$('fts').value.trim();return isQ(s)?s:''};   // 12.16: `field:value` text goes to the server as query= (parsed there), plain words stay search()
api('api/obs/search/info').then(i=>{if(i&&i.encrypted)$('fts').title=i.message}).catch(()=>{});   // 12.17: encrypted traces -> whole-word search only
function dsl(){const p=CHIPS.map(chipDSL);if(RAW)p.push(RAW);const s=$('fts').value.trim();if(s&&!isQ(s))p.push(`search(${q_(s)})`);if(MODE==='traces')p.push('eq(is_root,"true")');
 return p.length?(p.length>1?`and(${p.join(', ')})`:p[0]):''}
const opLabel=o=>[...OPS_S,...OPS_N].find(x=>x[1]===o)?.[0]||o;
function drawChips(){$('chips').innerHTML=CHIPS.map((c,i)=>`<span class="fchip"><b>${esc((ATTRS.find(a=>a[0]===c.attr)||[])[1]||c.attr)}${c.key?' · '+esc(c.key):''}</b><i>${esc(opLabel(c.op))}</i><span>${esc(c.value)}</span><button class="gh" onclick="CHIPS.splice(${i},1);drawChips();load()">${ICN('x')}</button></span>`).join('')}
$('addf').onclick=e=>{const m=aosPopup(e.currentTarget,`<div style="padding:8px;display:grid;gap:6px;width:300px"><div class="mh" style="padding:0">Add filter</div>
 <select id="fa">${ATTRS.map(a=>`<option value="${a[0]}">${a[1]}</option>`).join('')}</select><input id="fk" placeholder="Key" style="display:none"><select id="fo"></select><input id="fv" placeholder="Value" list="fvl"><datalist id="fvl"></datalist>
 <div class="row"><span class="sp"></span><button id="fok">Apply</button></div><hr style="border:0;border-top:1px solid var(--bd2);margin:2px 0"><a class="mi" id="fadv">${ICN('code')}Advanced (raw query)</a></div>`);
 const sync=()=>{const a=ATTRS.find(x=>x[0]===m.querySelector('#fa').value);m.querySelector('#fk').style.display=['fb','md'].includes(a[2])?'':'none';
  m.querySelector('#fo').innerHTML=(a[2]==='n'||a[2]==='fb'?OPS_N:a[2]==='fts'?[['contains','like']]:OPS_S).map(o=>`<option value="${o[1]}">${o[0]}</option>`).join('');
  const vals={run_type:['llm','chain','tool','retriever','prompt'],status:['success','error'],name:[...new Set(ROWS.map(r=>r.name))],model:[...new Set(ROWS.map(r=>r.model).filter(Boolean))],tag:[...new Set(ROWS.flatMap(r=>r.tags||[]))]}[a[0]]||[];
  m.querySelector('#fvl').innerHTML=vals.slice(0,30).map(v=>`<option value="${esc(v)}">`).join('')};
 m.querySelector('#fa').onchange=sync;sync();m.querySelector('#fv').focus();
 m.querySelector('#fok').onclick=()=>{const c={attr:m.querySelector('#fa').value,op:m.querySelector('#fo').value,value:m.querySelector('#fv').value,key:m.querySelector('#fk').value||undefined};if(c.value==='')return;CHIPS.push(c);m.remove();drawChips();load()};
 m.querySelector('#fadv').onclick=()=>{m.remove();$('raw').style.display='block';$('rawq').focus()}};
$('rawb').onclick=()=>{const r=$('raw');r.style.display=r.style.display==='none'?'block':'none';$('rawq').value=RAW||dsl()};
$('rawgo').onclick=()=>{RAW=$('rawq').value.trim();CHIPS=[];SORT='';drawChips();load()};
$('cpf').onclick=()=>{navigator.clipboard.writeText(dsl());flash('Filter copied')};
// GD7.2 "Hỏi bằng lời / Ask in words": the question becomes a filter (POST /api/obs/nl-filter, runs nothing), shown in the raw editor to review/edit; "Chạy / Run" applies it
const T_=(en,vi)=>LANG==='vi'?vi:en;let NLP=null,SORT='';
$('nlbt').textContent=T_('Ask in words','Hỏi bằng lời');$('nlq').placeholder=T_('e.g. errors in the last 24 hours, runs slower than 5s with gemini yesterday','vd: lỗi trong 24 giờ qua, run chậm hơn 5 giây dùng gemini hôm qua');
$('nlask').textContent=T_('Ask','Hỏi');$('nlrun').textContent=T_('Run','Chạy');
$('nlb').onclick=()=>{const n=$('nl');n.style.display=n.style.display==='none'?'block':'none';if(n.style.display==='block')$('nlq').focus()};
async function nlAsk(){const q=$('nlq').value.trim();if(q.length<2||!PROJ)return;$('nlerr').textContent='';$('nlask').disabled=true;$('nlex').textContent=T_('Thinking…','Đang hiểu câu hỏi…');
 try{const r=await api('api/obs/nl-filter','POST',{project_id:PROJ.id,question:q,lang:LANG==='vi'?'vi':'en'});NLP=r;
  $('raw').style.display='block';$('rawq').value=r.filter;$('nlex').textContent=(r.explanation||'')+(r.fields_used&&r.fields_used.length?'  ['+r.fields_used.join(', ')+']':'');$('nlrun').style.display='';$('rawq').focus()}
 catch(e){$('nlex').textContent='';$('nlerr').textContent=e.message}$('nlask').disabled=false}
$('nlask').onclick=nlAsk;$('nlq').onkeydown=e=>{if(e.key==='Enter')nlAsk()};
$('nlrun').onclick=()=>{const r=NLP||{};RAW=$('rawq').value.trim();CHIPS=[];drawChips();SORT=r.sort||'';
 if(r.since_hours){SINCE=Math.round(r.since_hours);$('tname').textContent=(TIMES.find(t=>t[0]===SINCE)||[0,SINCE+'h'])[1]}
 if(r.mode){MODE=r.mode;document.querySelectorAll('#mode button').forEach(x=>x.classList.toggle('on',x.dataset.m===MODE))}
 NLP=null;$('nlrun').style.display='none';load()};
$('fts').onkeydown=e=>{if(e.key==='Enter')load()};
// ---- time / mode / views
$('tbtn').onclick=e=>{const m=aosPopup(e.currentTarget,TIMES.map(([h,l])=>`<a class="mi" data-h="${h}">${l}${h===SINCE?' '+ICN('check'):''}</a>`).join(''));m.onclick=ev=>{const a=ev.target.closest('[data-h]');if(!a)return;SINCE=+a.dataset.h;$('tname').textContent=TIMES.find(t=>t[0]===SINCE)[1];m.remove();load()}};
document.querySelectorAll('#mode button').forEach(b=>b.onclick=()=>{MODE=b.dataset.m;document.querySelectorAll('#mode button').forEach(x=>x.classList.toggle('on',x===b));load()});
const viewState=()=>({chips:CHIPS,raw:RAW,since:SINCE,mode:MODE,fts:$('fts').value});
function applyView(v){VIEW=v;const s=v?v.state:{};CHIPS=s.chips||[];RAW=s.raw||'';SINCE=s.since??24;MODE=s.mode||'traces';$('fts').value=s.fts||'';$('vname').textContent=v?v.name:'Default View';$('savev').style.display=v?'':'none';
 $('tname').textContent=(TIMES.find(t=>t[0]===SINCE)||TIMES[1])[1];document.querySelectorAll('#mode button').forEach(x=>x.classList.toggle('on',x.dataset.m===MODE));drawChips();load()}
async function loadViews(){VIEWS=await api('api/ui/views?scope=tracing').catch(()=>[])}
$('vbtn').onclick=e=>{const m=aosPopup(e.currentTarget,`<div class="mh">Views</div><a class="mi" data-v="">Default View${VIEW?'':' '+ICN('check')}</a>`+VIEWS.map(v=>`<a class="mi" data-v="${v.id}"><span style="flex:1">${esc(v.name)}</span>${VIEW&&VIEW.id===v.id?ICN('check'):''}<button class="gh" data-del="${v.id}" title="Delete">${ICN('trash')}</button></a>`).join(''));
 m.onclick=async ev=>{const d=ev.target.closest('[data-del]');if(d){ev.stopPropagation();await api('api/ui/views/'+d.dataset.del,'DELETE');await loadViews();m.remove();if(VIEW&&VIEW.id==d.dataset.del)applyView(null);return}
  const a=ev.target.closest('[data-v]');if(!a)return;m.remove();applyView(VIEWS.find(v=>String(v.id)===a.dataset.v)||null)}};
$('saveas').onclick=async()=>{const n=prompt('View name');if(!n)return;const d=prompt('Description (optional)')||null;const r=await api('api/ui/views','POST',{name:n,description:d,state:viewState()});await loadViews();applyView(VIEWS.find(v=>v.id===r.id));flash('View saved')};
$('savev').onclick=async()=>{if(!VIEW)return;await api('api/ui/views/'+VIEW.id,'PUT',{name:VIEW.name,description:VIEW.description,state:viewState()});await loadViews();flash('View updated')};
$('fsb').onclick=()=>$('fs').classList.toggle('on');
// GĐ6.10 "Của ai": workspace admins can narrow chat traces/threads to one user (everyone else only ever sees their own)
(async()=>{try{const m=await api('api/auth/me');if(m.role!=='admin')return;const us=await api('api/admin/users');$('own').innerHTML='<option value="">Của ai: tất cả</option>'+us.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join('');$('own').style.display='';$('own').onchange=()=>{OWN=$('own').value;load()}}catch(e){}})();
// ---- table
const COLS={traces:['','','Name','Input','Output','Error','Start Time','Latency','Tokens','Cost','Feedback','Tags','Của ai'],threads:['','Turns','First Input','Last Output','Thread ID','Last Error','First Start Time','Last Start Time','P50 Latency','Tokens','Cost','Của ai']};
function head(){const c=MODE==='threads'?COLS.threads:COLS.traces;$('rh').innerHTML='<tr>'+c.map((h,i)=>i===0?`<th style="width:28px"><input type="checkbox" id="ckall"></th>`:`<th>${h}</th>`).join('')+'</tr>';
 $('ckall').onclick=e=>{document.querySelectorAll('.rk').forEach(c=>{c.checked=e.target.checked;c.checked?SELIDS.add(c.value):SELIDS.delete(c.value)});bulk()}}
async function load(){if(!PROJ)return;SELIDS.clear();bulk();head();$('ferr').textContent='';$('rb').innerHTML=`<tr><td colspan="13" class="muted">${ICN('loader')} Loading…</td></tr>`;
 try{if(MODE==='threads'){const ts=await api('api/obs/projects/'+PROJ.id+'/threads?limit=200'+(OWN?'&owner_id='+OWN:'')+(dsl()?'&filter='+encodeURIComponent(dsl()):'')+(qtext()?'&query='+encodeURIComponent(qtext()):'')+(RAW&&SINCE?'&since_hours='+SINCE:''));ROWS=[];
   $('rb').innerHTML=ts.map(t=>`<tr class="r" data-th="${esc(t.thread_id)}"><td><input type="checkbox" class="rk" value="th:${esc(t.thread_id)}"></td><td><span class="chip">${ICN('arrow-left-right')}${t.turns}</span></td><td><div class="trunc" translate="no">${esc(preview(t.first_input||t.last_input))}</div></td><td><div class="trunc" translate="no">${esc(preview(t.last_output))}</div></td>
   <td class="mono" style="font-size:12px">${esc(String(t.thread_id).slice(0,18))}</td><td class="err">${t.errors?t.errors+' error'+(t.errors>1?'s':''):''}</td><td>${dt(t.first)}</td><td>${dt(t.last)}</td><td>${latPill(t.p50)}</td><td>${fmtN(t.tokens||0)}</td><td>${t.cost?'$'+Number(t.cost).toFixed(4):''}</td><td>${esc(t.owner||'')}</td></tr>`).join('')||empty('No threads','Traces need a thread_id or session_id in metadata to be grouped into threads.');
   $('more').innerHTML='';return}
  const p=new URLSearchParams({project_id:PROJ.id,filter:dsl(),root_only:MODE==='traces',limit:200});if(SINCE)p.set('since_hours',SINCE);if(OWN)p.set('owner_id',OWN);if(SORT)p.set('sort',SORT);if(qtext())p.set('query',qtext());
  ROWS=await api('api/obs/runs?'+p);
  $('rb').innerHTML=ROWS.map((r,i)=>`<tr class="r" data-i="${i}"><td><input type="checkbox" class="rk" value="${r.id}"></td><td>${stIcon(r.status)}</td><td><div class="nm">${rtTile(r.run_type)}${esc(r.name)}</div></td>
   <td><div class="trunc" translate="no">${esc(preview(r.inputs))}</div></td><td><div class="trunc" translate="no">${esc(preview(r.outputs))}</div></td><td><div class="trunc err">${esc(r.error||'')}</div></td><td style="white-space:nowrap">${dt(r.start_time)}</td><td>${latPill(r.latency_ms)}</td>
   <td>${r.total_tokens?`<span class="chip">${ICN('coins')}${fmtN(r.total_tokens)}</span>`:''}</td><td title="${r.cost_usd?'Total $'+Number(r.cost_usd).toFixed(6):''}">${r.cost_usd?'$'+Number(r.cost_usd).toFixed(4):''}</td>
   <td>${(r.feedback||[]).map(f=>`<span class="chip pu">${esc(f.key)}: ${f.score??''}</span>`).join('')}</td><td>${(r.tags||[]).slice(0,3).map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</td><td>${esc(r.owner||'')}</td></tr>`).join('')||empty('No runs found','Try a wider time range or remove a filter.');
  $('more').innerHTML=`<small class="muted">${ROWS.length} ${MODE==='runs'?'runs':'traces'}${ROWS.length>=200?' (first 200)':''}</small>`;shortcuts()}
 catch(e){$('rb').innerHTML='';$('raw').style.display='block';$('ferr').textContent=e.message}}
const empty=(t,s)=>`<tr><td colspan="13"><div class="empty"><span class="tile">${ICN('activity')}</span><b>${t}</b>${s}</div></td></tr>`;
$('rb').onclick=e=>{const ck=e.target.closest('.rk');if(ck){ck.checked?SELIDS.add(ck.value):SELIDS.delete(ck.value);bulk();return}
 const tr=e.target.closest('tr.r');if(!tr)return;if(tr.dataset.th){openThread(tr.dataset.th);return}const r=ROWS[+tr.dataset.i];
 if(e.metaKey||e.ctrlKey){window.open(location.pathname+'?p='+PROJ.id+'&trace='+r.trace_id,'_blank');return}CURI=+tr.dataset.i;openTrace(r.trace_id,MODE==='runs'?r.id:null)};
function shortcuts(){const g=(lbl,key,vals)=>{const c={};vals.forEach(v=>{if(v!=null&&v!=='')c[v]=(c[v]||0)+1});const e=Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,6);
  return e.length?`<div class="grp"><b>${lbl}</b>${e.map(([v,n])=>`<a data-a="${key}" data-v="${esc(v)}"><span class="trunc" style="max-width:160px">${esc(v)}</span><span class="muted">${n}</span></a>`).join('')}</div>`:''};
 $('fs').innerHTML='<div class="h4" style="margin-bottom:8px">Filter Shortcuts</div>'+g('Run name','name',ROWS.map(r=>r.name))+g('Run type','run_type',ROWS.map(r=>r.run_type))+g('Status','status',ROWS.map(r=>r.status))+g('Model','model',ROWS.map(r=>r.model))+g('Tag','tag',ROWS.flatMap(r=>r.tags||[]));
 $('fs').onclick=e=>{const a=e.target.closest('[data-a]');if(!a)return;CHIPS.push({attr:a.dataset.a,op:'eq',value:a.dataset.v});drawChips();load()}}
// ---- bulk bar
function bulk(){const n=SELIDS.size;$('bulk').classList.toggle('on',n>0);const th=[...SELIDS].some(x=>x.startsWith('th:'));$('bn').textContent=`${n} ${th?'thread':MODE==='runs'?'run':'trace'}${n>1?'s':''} selected`}
$('bx').onclick=()=>{SELIDS.clear();document.querySelectorAll('.rk,#ckall').forEach(c=>c.checked=false);bulk()};
async function selRunIds(){const out=[];for(const x of [...SELIDS].slice(0,100)){if(x.startsWith('th:')){const rs=await api('api/obs/projects/'+PROJ.id+'/threads/'+encodeURIComponent(x.slice(3)));rs.forEach(r=>out.push(r.id))}else out.push(x)}return out.slice(0,100)}
function pickPop(anchor,title,list,onPick,extra=''){const m=aosPopup(anchor,`<div style="padding:6px;width:300px"><div class="mh" style="padding:0 0 6px">${title}</div>${extra}<input id="pq" placeholder="Search" style="width:100%;margin-bottom:4px"><div id="pl2" style="max-height:260px;overflow:auto"></div></div>`,{up:anchor.closest('.bulk')!=null});
 const draw=q=>{m.querySelector('#pl2').innerHTML=list.filter(x=>!q||x.name.toLowerCase().includes(q)).map(x=>`<a class="mi" data-id="${x.id}"><span style="flex:1">${esc(x.name)}</span><small class="muted">${x.sub||''}</small></a>`).join('')||'<div class="mi muted">None</div>'};
 draw('');m.querySelector('#pq').oninput=e=>draw(e.target.value.toLowerCase());m.onclick=ev=>{const a=ev.target.closest('[data-id]');if(a){m.remove();onPick(a.dataset.id,m)}};return m}
$('baq').onclick=e=>pickPop(e.currentTarget,'Select Annotation Queue',QUEUES.map(q=>({id:q.id,name:q.name,sub:q.pending+' pending'})),async id=>{const r=await selRunIds();await api('api/queues/'+id+'/items','POST',{run_ids:r});flash(`Added ${r.length} to annotation queue`);$('bx').onclick()});
$('bds').onclick=e=>pickPop(e.currentTarget,'Add to Dataset',DSETS.map(d=>({id:d.id,name:d.name,sub:d.n+' examples'})),async id=>{const ths=[...SELIDS].filter(x=>x.startsWith('th:')).map(x=>x.slice(3));if(ths.length&&ths.length===SELIDS.size){const r=await api('api/datasets/'+id+'/from_threads','POST',{project_id:PROJ.id,thread_ids:ths});flash(`Added ${r.added} of ${ths.length} threads (1 example each)`)}else{const r=await selRunIds();await api('api/datasets/'+id+'/from_runs','POST',{run_ids:r});flash(`Added ${r.length} to dataset`)}$('bx').onclick()});
// ---- trace slide-over
let CUR=null,CURI=-1,SELR=null,PVIEW='details',RTAB='input',WATER=false,FMT='pretty';
$('pclose').onclick=()=>{$('pane').classList.remove('on');history.replaceState(null,'','?p='+PROJ.id)};$('pfull').onclick=()=>$('pane').classList.toggle('full');
$('pprev').onclick=()=>{if(CURI>0){CURI--;openTrace(ROWS[CURI].trace_id)}};$('pnext').onclick=()=>{if(CURI<ROWS.length-1){CURI++;openTrace(ROWS[CURI].trace_id)}};
$('prf').onclick=()=>CUR&&openTrace(CUR.tid,SELR);
document.querySelectorAll('#pview button').forEach(b=>b.onclick=()=>{if(b.disabled)return;PVIEW=b.dataset.v;document.querySelectorAll('#pview button').forEach(x=>x.classList.toggle('on',x===b));renderPane()});
document.addEventListener('keydown',e=>{if(!$('pane').classList.contains('on')||e.target.matches('input,textarea,select'))return;if(e.key==='Escape')$('pclose').onclick();
 const k={t:'trajectory',m:'messages',d:'details'}[e.key.toLowerCase()];if(k)document.querySelector(`#pview [data-v="${k}"]`).click();if(e.key==='ArrowDown'&&e.altKey)$('pnext').onclick();if(e.key==='ArrowUp'&&e.altKey)$('pprev').onclick()});
async function openTrace(tid,runId){const d=await api('api/obs/traces/'+tid);const root=d.runs.find(r=>!r.parent_id)||d.runs[0];CUR={tid,...d,root,thread:null};SELR=runId||root.id;
 if(root.thread_id){try{CUR.thread=await api('api/obs/projects/'+PROJ.id+'/threads/'+encodeURIComponent(root.thread_id))}catch(e){}}
 try{CUR.att=await api('api/obs/traces/'+tid+'/attachments')}catch(e){CUR.att=[]}
 $('ptitle').innerHTML=root.thread_id?`${ICN('messages-square')}Thread ${idChip(root.thread_id)}`:`${ICN('list-tree')}Trace ${idChip(tid)}`;
 $('pstudio').href='studio'+(root.thread_id?'?thread='+encodeURIComponent(root.thread_id):'');
 prepTrace();document.querySelectorAll('#pview button').forEach(x=>x.classList.toggle('on',x.dataset.v===PVIEW));
 $('pane').classList.add('on');history.replaceState(null,'','?p='+PROJ.id+'&trace='+tid);renderPane()}
async function openThread(th){const rs=await api('api/obs/projects/'+PROJ.id+'/threads/'+encodeURIComponent(th));if(!rs.length)return;PVIEW='turns';await openTrace(rs[rs.length-1].trace_id)}
$('paq').onclick=e=>pickPop(e.currentTarget,'Add to Annotation Queue',QUEUES.map(q=>({id:q.id,name:q.name})),async id=>{await api('api/queues/'+id+'/items','POST',{run_ids:[SELR]});flash('Added to annotation queue')});
//<<pure 12.24 (no DOM: tests/test_gd12_trace_view.py runs these blocks in node)
// untrusted channel text (11.72 `[UNTRUSTED DATA (src) ...] ... [END UNTRUSTED DATA]`) is shown as the sender's text + a small badge, never the raw wrapper
const UNT_HEAD=/^\s*\[UNTRUSTED DATA(?:\s*\(([^)\n]*)\))?[^\]\n]*\]\n?/,UNT_TAIL=/\n?\[END UNTRUSTED DATA\]\s*$/;
function unwrapExt(s){if(typeof s!=='string')return{text:s,ext:null};const m=UNT_HEAD.exec(s);if(!m)return{text:s,ext:null};return{text:s.slice(m[0].length).replace(UNT_TAIL,''),ext:(m[1]||'').trim()||'external'}}
const uw=s=>typeof s==='string'?unwrapExt(s).text:s;
// images: only data: rasters and same-origin URLs are ever put in an <img>; everything else is shown as a label (the page never requests it)
function safeImg(u){u=String(u||'');if(/^data:image\/(png|jpe?g|gif|webp|bmp|avif);/i.test(u))return true;try{const o=new URL(u,location.href);return(o.protocol==='http:'||o.protocol==='https:')&&o.origin===location.origin}catch(e){return false}}
function msgs(v){if(!v)return[];if(typeof v==='string'){try{v=JSON.parse(v)}catch(e){return[]}}const out=[];
 const push=(role,content)=>{if(content==null||content==='')return;
  if(typeof content==='object'&&!Array.isArray(content)){out.push({role,content:JSON.stringify(content)});return}
  const text=[],think=[],imgs=[];let ext=null;
  (Array.isArray(content)?content:[content]).forEach(c=>{
   if(typeof c==='string'){const u=unwrapExt(c);text.push(u.text);ext=ext||u.ext;return}
   if(!c||typeof c!=='object')return;const t=String(c.type||'');
   if(/^(thinking|reasoning|reasoning_content)$/.test(t)){think.push(c.thinking||c.reasoning||c.text||(Array.isArray(c.summary)?c.summary.map(x=>x.text||'').join(''):''));return}
   if(/^(image_url|image|input_image)$/.test(t)){const iu=typeof c.image_url==='string'?c.image_url:(c.image_url&&c.image_url.url)||c.url||'',sc=c.source;const u=iu||(sc&&(sc.url||(sc.data?'data:'+(sc.media_type||'image/png')+';base64,'+sc.data:'')))||'';if(u)imgs.push(u);return}
   if(typeof c.text==='string'){const u=unwrapExt(c.text);text.push(u.text);ext=ext||u.ext}});
  const tt=think.join('\n').trim();if(tt)out.push({role:'reasoning',content:tt,think:true});
  const body=text.join('');if(body!==''||imgs.length)out.push({role,content:body,...(ext?{ext}:{}),...(imgs.length?{imgs}:{})})};
 let arr=v.messages||(Array.isArray(v)?v:null);if(arr&&Array.isArray(arr[0]))arr=arr[0];
 if(Array.isArray(arr))arr.forEach(m=>{if(m&&typeof m==='object')push(m.role||m.type||(m.id&&m.id.slice&&m.id.slice(-1)[0])||'message',m.content??m.kwargs?.content??m.text)});
 else{if(v.message||v.question)push('user',v.message||v.question);if(v.answer||v.reply)push('ai',v.answer||v.reply);if(v.generations)push('ai',(v.generations.flat()[0]||{}).text)}
 return out}
//>>pure
const ROLE={user:'User',human:'User',ai:'AI',assistant:'AI',system:'System',tool:'Tool'};
const extBadge=m=>m&&m.ext?`<span class="chip ext" title="${esc(T_('Text from outside (untrusted source)','Nội dung từ bên ngoài (nguồn không tin cậy)'))}${m.ext!=='external'?': '+esc(m.ext):''}">${ICN('globe')}${T_('external','nguồn ngoài')}</span>`:'';
const imgsHtml=l=>(l&&l.length)?`<div class="imgs">${l.map(u=>safeImg(u)?`<img class="thumb" src="${esc(u)}" alt="" loading="lazy">`:`<span class="chip" title="${esc(T_('External image: not loaded by this page','Ảnh bên ngoài: trang này không tải'))}">${ICN('globe')}${T_('external image (not loaded)','ảnh ngoài (không tải)')}</span>`).join('')}</div>`:'';
function bubbles(ms){return ms.map(m=>{if(m.think)return `<div class="bub"><details style="min-width:0"><summary>${ICN('sparkles')} ${T_('Reasoning','Suy luận')}</summary><div class="tx">${esc(m.content)}</div></details></div>`;
 const r=ROLE[String(m.role).toLowerCase()]||m.role;return `<div class="bub"><span class="who">${esc(String(r).slice(0,2).toUpperCase())}</span><div style="min-width:0"><div class="rl">${esc(r)}${extBadge(m)}</div><div class="tx">${esc(m.content)}</div>${imgsHtml(m.imgs)}</div></div>`}).join('')}
function yaml(o,ind=''){if(o===null||typeof o!=='object')return JSON.stringify(o);if(Array.isArray(o))return o.map(x=>`\n${ind}- `+yaml(x,ind+'  ')).join('');return Object.entries(o).map(([k,v])=>`\n${ind}${k}: `+(v&&typeof v==='object'?yaml(v,ind+'  '):JSON.stringify(v))).join('')}
function ioBlock(v,label){const ms=msgs(v);const body=FMT==='pretty'&&ms.length?bubbles(ms):`<pre>${esc(FMT==='yaml'?yaml(v).trim():JSON.stringify(v,null,2))}</pre>`;
 return `<details class="sec" open><summary>${ICN('chevron-down')}${label}<span class="sp"></span><select class="fmt" style="height:24px;font-size:12px" onclick="event.stopPropagation()">${['pretty','json','yaml'].map(f=>`<option value="${f}" ${f===FMT?'selected':''}>${f==='pretty'?'Pretty':f.toUpperCase()}</option>`).join('')}</select>
 <button class="gh" title="Copy" onclick="event.preventDefault();navigator.clipboard.writeText(${esc(JSON.stringify(JSON.stringify(v)))});flash('Copied')">${ICN('copy')}</button></summary>${body}</details>`}
function flat(o,p='',out=[]){if(o&&typeof o==='object'&&!Array.isArray(o)){for(const [k,v] of Object.entries(o))flat(v,p?p+'.'+k:k,out)}else out.push([p,o]);return out}
function renderPane(){const b=$('pbody');if(PVIEW==='trajectory')renderTraj(b);else if(PVIEW==='messages')renderMsgs(b);else if(PVIEW==='turns')renderTurns(b);else renderDetails(b);searchApply()}
function renderDetails(b){
 const runs=CUR.runs,root=CUR.root,kids={};runs.forEach(r=>{(kids[r.parent_id||'root']=kids[r.parent_id||'root']||[]).push(r)});const t0=new Date(root.start_time).getTime(),tot=Math.max(root.latency_ms||1,1);
 const llmTok=runs.filter(r=>r.run_type==='llm').reduce((a,r)=>a+(r.total_tokens||0),0)||root.total_tokens||0;
 const node=(r,d)=>{const l=(new Date(r.start_time).getTime()-t0)/tot*100,w=Math.max((r.latency_ms||0)/tot*100,.5);
  return `<div class="tn ${r.id===SELR?'on':''}" data-id="${r.id}" data-k="${r._k}" style="padding-left:${10+d*16}px" data-name="${esc(r.name.toLowerCase())}"><div class="l1">${rtTile(r.run_type)}<span class="trunc" style="color:var(--t1)">${esc(r.name)}</span>${r.status==='error'?`<span class="st-er">${ICN('circle-alert')}</span>`:''}${d===0?`<small class="muted">${dt(r.start_time)}</small>`:''}</div>
  <div class="l2" style="padding-left:26px">${latPill(r.latency_ms)}${tokCost(r.total_tokens,r.cost_usd)}${r.model?`<span class="chip">${esc(r.model)}</span>`:''}</div>${WATER?`<div class="wf" style="margin-left:26px"><div style="left:${Math.min(l,99)}%;width:${Math.min(w,100)}%"></div></div>`:''}</div>`+(kids[r.id]||[]).map(c=>node(c,d+1)).join('')};
 b.innerHTML=`<div class="tree"><div class="th"><b style="font-size:13px">${CUR.root.thread_id?'Turns':'Trace'}</b><span class="sp"></span><button class="g ${WATER?'on':''}" id="wfb" style="height:26px">${ICN('chart-column')}Waterfall</button></div>
  <div style="padding:6px 10px"><input id="tf" placeholder="Filter runs…" style="width:100%;height:28px"></div>
  <div class="sum"><b style="color:var(--t1)">Summary</b><span>${ICN('list-tree')} ${runs.length}</span><span>P50: ${fmtS(root.latency_ms)}</span><span>${ICN('coins')} ${fmtN(llmTok)} / $${Number(root.cost_usd||0).toFixed(6)}</span>${ttftChip(turnStats(root,runs))}</div>${truncNote()}<div class="rows">${kids.root?kids.root.map(r=>node(r,0)).join(''):node(root,0)}</div></div><div class="det" id="det"></div>`;
 $('wfb').onclick=()=>{WATER=!WATER;renderPane()};$('tf').oninput=e=>{const q=e.target.value.toLowerCase();b.querySelectorAll('.tn').forEach(n=>n.style.display=!q||n.dataset.name.includes(q)?'':'none')};
 b.querySelector('.rows').onclick=e=>{const n=e.target.closest('.tn');if(!n)return;SELR=n.dataset.id;b.querySelectorAll('.tn').forEach(x=>x.classList.toggle('on',x===n));renderRun()};renderRun()}
function renderRun(){const r=CUR.runs.find(x=>x.id===SELR)||CUR.root;const fb=(CUR.feedback||[]).filter(f=>f.run_id===r.id);const md={...(r.metadata||{}),run_type:r.run_type,model:r.model,input_tokens:r.input_tokens,output_tokens:r.output_tokens,total_tokens:r.total_tokens,cost_usd:r.cost_usd,start_time:r.start_time,end_time:r.end_time,thread_id:r.thread_id,tags:(r.tags||[]).join(', '),environment:r.environment};
 const xp=(r.metadata||{}).external_parent;
 const turn=CUR.thread?CUR.thread.findIndex(t=>t.trace_id===CUR.tid)+1:0;
 $('det').innerHTML=`<div class="row" style="flex-wrap:nowrap">${rtTile(r.run_type)}<b style="font-size:15px">${esc(r.name)}</b>${idChip(String(r.id).slice(0,8)+'…')}${turn?`<span class="chip">Turn ${turn}</span>`:''}<span class="sp"></span>
  <button class="g" id="flag" title="Flag for Engine">${ICN('flag')}</button><button class="g" id="addto" data-menu="1">${ICN('plus')}Add to${ICN('chevron-down')}</button><button class="g" id="shr" title="Share">${ICN('share')}</button></div>
  ${r.error?`<pre class="err" style="margin-top:8px">${esc(r.error)}</pre>`:''}
  ${xp?`<div class="xp" id="xparent">${ICN('external-link')}<b>${T_('Continues an external trace','Tiếp nối một trace bên ngoài')}</b><span>${T_('parent span','span cha')} <code>${esc(xp.span_id)}</code></span><span>${T_('trace','trace')} <code>${esc(xp.trace_id)}</code></span>${xp.tracestate?`<span>tracestate <code>${esc(xp.tracestate)}</code></span>`:''}<button class="g" data-cp="${esc(xp.traceparent||'')}" onclick="navigator.clipboard.writeText(this.dataset.cp);flash('Copied')">${ICN('copy')}traceparent</button></div>`:''}
  <div class="ptabs" style="margin-top:10px">${['feedback','input','output','attributes'].map(t=>`<button data-rt="${t}" class="${t===RTAB?'on':''}">${t[0].toUpperCase()+t.slice(1)}</button>`).join('')}</div>
  <div id="rtab"></div>`;
 const draw=()=>{const box=$('rtab');
  const fbH=`<details class="sec" open id="sec_feedback"><summary>${ICN('chevron-down')}Feedback</summary><div class="row" style="margin-top:6px">${fb.map(f=>`<span class="chip pu" title="${esc(f.comment||'')}">${esc(f.key)}: ${f.score??esc(f.value??'')}</span>`).join('')||'<span class="muted">No feedback yet.</span>'}</div>
   <div class="row" style="margin-top:8px"><input id="fbk" placeholder="Key" value="user_score" style="width:140px"><input id="fbs" type="number" step="any" placeholder="Score" style="width:90px"><input id="fbc" placeholder="Comment" style="flex:1"><button class="g" id="fbb">${ICN('plus')}Add feedback</button></div></details>`;
  const atH=`<details class="sec" open id="sec_attributes"><summary>${ICN('chevron-down')}Attributes</summary><div class="h4" style="margin:8px 0 4px">Metadata</div><div class="kv">${flat(md).filter(([k,v])=>v!=null&&v!=='').map(([k,v])=>`<div class="k">${esc(k)}</div><div class="v">${esc(typeof v==='object'?JSON.stringify(v):v)}<span class="act"><button class="gh" title="Copy" data-cp="${esc(String(v))}">${ICN('copy')}</button><button class="gh" title="Filter by this value" data-fk="${esc(k)}" data-fv="${esc(String(v))}">${ICN('filter')}</button></span></div>`).join('')}</div></details>`;
  box.innerHTML=fbH+`<div id="sec_input">${ioBlock(r.inputs,'Input')}</div><div id="sec_output">${ioBlock(r.outputs,'Output')}</div>`+atH;
  $('fbb').onclick=async()=>{await api('api/obs/feedback','POST',{run_id:r.id,key:$('fbk').value,score:$('fbs').value===''?null:+$('fbs').value,comment:$('fbc').value||null});flash('Feedback saved');openTrace(CUR.tid,r.id)};
  box.querySelectorAll('.fmt').forEach(s=>s.onchange=()=>{FMT=s.value;draw()});
  box.onclick=e=>{const c=e.target.closest('[data-cp]');if(c){navigator.clipboard.writeText(c.dataset.cp);flash('Copied')}const f=e.target.closest('[data-fk]');if(f){const k=f.dataset.fk;const base=['name','run_type','status','model','thread_id'];
   CHIPS.push(base.includes(k)?{attr:k,op:'eq',value:f.dataset.fv}:{attr:'metadata',key:k,op:'eq',value:f.dataset.fv});drawChips();$('pclose').onclick();load()}}};
 draw();document.querySelectorAll('[data-rt]').forEach(b=>b.onclick=()=>{RTAB=b.dataset.rt;document.querySelectorAll('[data-rt]').forEach(x=>x.classList.toggle('on',x===b));const t=$('sec_'+RTAB);if(t)t.scrollIntoView({behavior:'smooth',block:'start'})});
 $('addto').onclick=e=>{const m=aosPopup(e.currentTarget,`<a class="mi" data-k="ds">${ICN('database')}Add to Dataset</a><a class="mi" data-k="aq">${ICN('square-pen')}Add to Annotation Queue</a>`,{right:true});
  m.onclick=ev=>{const a=ev.target.closest('[data-k]');if(!a)return;m.remove();const anc=$('addto');if(a.dataset.k==='ds')pickPop(anc,'Add to Dataset',DSETS.map(d=>({id:d.id,name:d.name})),async id=>{await api('api/datasets/'+id+'/from_runs','POST',{run_ids:[r.id]});flash('Added to dataset')});
   else pickPop(anc,'Add to Annotation Queue',QUEUES.map(q=>({id:q.id,name:q.name})),async id=>{await api('api/queues/'+id+'/items','POST',{run_ids:[r.id]});flash('Added to annotation queue')})}};
 $('shr').onclick=async()=>{const s=await api('api/obs/share','POST',{trace_id:CUR.tid,hours:72});const u=location.origin+s.path;navigator.clipboard.writeText(u).catch(()=>{});flash('Public link copied (expires in 72 hours)')};
 $('flag').onclick=async()=>{await api('api/obs/feedback','POST',{run_id:r.id,key:'engine_flag',score:1,comment:'Flagged for Engine'});flash('Flagged for Engine')}}
// ---- GĐ9.4: Trajectory (ordered steps) + Messages (chat bubbles) + search inside a trace. Built only from the spans already loaded (no new API/table).
const CAP_STEP=150;let CAPS={trajectory:CAP_STEP,messages:CAP_STEP},SQ='',SI=0,SMATCH=[];
//<<pure 12.24
const pj=v=>{if(typeof v==='string'){try{return JSON.parse(v)}catch(e){}}return v};
const jtxt=v=>v==null?'':typeof v==='string'?v:JSON.stringify(v);
const cut=(s,n)=>{s=String(s??'').replace(/\s+/g,' ').trim();return s.length>n?s.slice(0,n-1)+'…':s};
const argSum=v=>{v=pj(v);const a=v&&typeof v==='object'&&!Array.isArray(v)?(v.args&&typeof v.args==='object'&&!Array.isArray(v.args)?v.args:v):null;return a?Object.entries(a).map(([k,x])=>k+'='+cut(jtxt(x),48)).join(', '):cut(jtxt(v),140)};
//>>pure
const sq_=()=>SQ.trim().toLowerCase();
function hl(s){s=String(s??'');const q=sq_();if(!q)return esc(s);const l=s.toLowerCase();let out='',i=0;for(;;){const j=l.indexOf(q,i);if(j<0)break;out+=esc(s.slice(i,j))+'<mark>'+esc(s.slice(j,j+q.length))+'</mark>';i=j+q.length}return out+esc(s.slice(i))}
//<<pure 12.24
const hay=(...a)=>a.map(jtxt).join(' \n ').toLowerCase();
//>>pure
const KIND_LBL=()=>({run:T_('Run','Lần chạy'),model:T_('Model call','Gọi model'),tool:T_('Tool call','Gọi công cụ'),agent:T_('Sub-agent','Agent con'),approval:T_('Approval','Duyệt'),interrupt:T_('Interrupt','Tạm dừng chờ duyệt'),error:T_('Error','Lỗi'),other:T_('Step','Bước')});
const KIND_ICON={run:'play',model:'sparkles',tool:'wrench',agent:'git-branch',approval:'circle-check',interrupt:'pause',error:'circle-alert',other:'box'};
//<<pure 12.24
const ms_=t=>t?new Date(t).getTime():NaN;
function parseDocs(o){o=pj(o);const a=o&&(o.documents||o.docs||o.output||o.result||o);if(!Array.isArray(a))return[];
 return a.map((d,i)=>{if(typeof d==='string')return{title:'#'+(i+1),text:cut(uw(d),200)};if(!d||typeof d!=='object')return null;const k=d.kwargs||d,md=k.metadata||d.metadata||{},tx=k.page_content??k.content??k.text??'';
  return{title:String(md.title||md.source||md.name||md.file||'#'+(i+1)),text:cut(uw(typeof tx==='string'?tx:jtxt(tx)),200)}}).filter(Boolean)}
// per-turn numbers: tokens, cost, latency, TTFT (first_token_time - start_time, 12.15), tokens/second (only when TTFT and output tokens are known)
function turnStats(root,mem){const llm=mem.filter(r=>r.run_type==='llm'),sum=f=>llm.reduce((a,r)=>a+(+r[f]||0),0);
 const tokens=root.total_tokens||sum('total_tokens')||0,cost=+root.cost_usd||sum('cost_usd'),lat=root.latency_ms,s0=ms_(root.start_time);let ft=ms_(root.first_token_time);if(!(ft>=s0))ft=NaN;
 if(isNaN(ft))llm.forEach(r=>{const f=ms_(r.first_token_time);if(f>=s0&&(isNaN(ft)||f<ft))ft=f});
 const ttft=isNaN(ft)?null:ft-s0,out=root.output_tokens||sum('output_tokens');
 return{tokens,cost,lat,ttft,tps:ttft!=null&&out&&lat>ttft?out/((lat-ttft)/1000):null}}
// Messages = conversation: per turn the user text, then the agent's steps in order. Sub-agents (ls_agent_type='subagent' or metadata.lc_agent_name) are nested blocks with their own cost;
// tools of the same parent that overlap in time share a `grp`; reasoning blocks and ls_message_view_exclude / middleware spans stay out of the flow; retrievers list their documents.
function buildMsgs(runs){const by={};runs.forEach(r=>by[r.id]=r);
 const isRoot=r=>!by[r.parent_id]||!!(r.metadata&&r.metadata.resumed_from);
 const rootOf=r=>{let p=r,n=0;while(!isRoot(p)&&n++<50)p=by[p.parent_id];return p};
 const mdk=r=>{const m=r.metadata||{};return m.ls_agent_type==='subagent'?'s':m.lc_agent_name?'n:'+m.lc_agent_name:''};
 const isSub=r=>{if(isRoot(r))return false;const k=mdk(r);return !!k&&k!==mdk(by[r.parent_id])};
 const excl=r=>{let p=r,n=0;while(p&&n++<50){const m=p.metadata||{};if(m.ls_message_view_exclude===true||m.ls_message_view_exclude==='true'||(p.run_type!=='llm'&&/middleware/i.test(p.name||'')))return true;if(isRoot(p))break;p=by[p.parent_id]}return false};
 const pathOf=r=>{const a=[];if(isRoot(r))return a;let p=by[r.parent_id],n=0;while(p&&n++<50){if(isSub(p))a.unshift(p.id);if(isRoot(p))break;p=by[p.parent_id]}return a};
 const roots=runs.filter(isRoot).sort((a,b)=>ms_(a.start_time)-ms_(b.start_time)),subs={};
 runs.filter(isSub).forEach(r=>{const inside=runs.filter(x=>{let p=x,n=0;while(p&&n++<50){if(p.id===r.id)return true;if(isRoot(p))return false;p=by[p.parent_id]}return false}).filter(x=>x.run_type==='llm');
  subs[r.id]={name:(r.metadata||{}).lc_agent_name||r.name,tokens:r.total_tokens||inside.reduce((a,x)=>a+(+x.total_tokens||0),0),cost:+r.cost_usd||inside.reduce((a,x)=>a+(+x.cost_usd||0),0),lat:r.latency_ms}});
 const M=[],mk=(role,content,run,x={})=>{const rr=by[run];const e={role,content:String(content??''),run,turn:rr?rootOf(rr).id:null,path:rr?pathOf(rr):[],hay:hay(content,x.name,role,x.args,(x.docs||[]).map(d=>d.title+' '+d.text)),...x};M.push(e);return e};
 const docEntry=r=>{const docs=parseDocs(r.outputs);mk('tool',docs.map(d=>d.title+': '+d.text).join('\n'),r.id,{name:r.name,docs,retr:true})};
 const llms=runs.filter(r=>r.run_type==='llm'&&!excl(r)&&msgs(r.inputs).length);
 if(llms.length){const seen={};
  [...llms,...runs.filter(r=>r.run_type==='retriever'&&!excl(r))].sort((a,b)=>ms_(a.start_time)-ms_(b.start_time)).forEach(r=>{
   if(r.run_type==='retriever')return docEntry(r);
   const key=pathOf(r).join('>'),a=msgs(r.inputs),n=a.filter(m=>!m.think).length,sn=seen[key]||0,put=m=>mk(m.role,m.content,r.id,{ext:m.ext,imgs:m.imgs,think:m.think,hist:true});   // `seen` counts real messages only (reasoning blocks are not in the next call's history)
   let from=0;if(n>=sn)for(let c=0;from<a.length&&c<sn;from++)if(!a[from].think)c++;
   a.slice(from).forEach(put);seen[key]=n;msgs(r.outputs).forEach(m=>{put(m);if(!m.think)seen[key]++})})}
 else{const kids={};runs.forEach(r=>{if(!isRoot(r))(kids[r.parent_id]=kids[r.parent_id]||[]).push(r)});Object.values(kids).forEach(a=>a.sort((x,y)=>ms_(x.start_time)-ms_(y.start_time)));
  const endOf=r=>{const e=ms_(r.end_time);return isNaN(e)?ms_(r.start_time)+(+r.latency_ms||0):e};
  const walk=p=>{let cl=null,pend=[];const flush=()=>{const l=pend;pend=[];l.forEach(walk)};
   (kids[p.id]||[]).forEach(c=>{if(excl(c)){flush();cl=null;return}
    if(c.run_type==='tool'){const s=ms_(c.start_time),e=endOf(c),to=pj(c.outputs);
     const ent=mk('tool',c.status==='error'?(c.error||''):jtxt(to&&to.result!==undefined?to.result:to),c.id,{name:c.name,args:argSum(c.inputs),err:c.status==='error'});
     if(cl&&s<cl.end){ent.grp=cl.id;cl.first.grp=cl.id;cl.end=Math.max(cl.end,e)}else{flush();cl={id:'g'+c.id,end:e,first:ent}}pend.push(c)}
    else{flush();cl=null;if(c.run_type==='retriever')docEntry(c);else walk(c)}});flush()};
  roots.forEach(root=>{const i=pj(root.inputs),o=pj(root.outputs),q=i&&(i.message||i.question);
   if(q){const u=unwrapExt(typeof q==='string'?q:jtxt(q));mk('human',u.text,root.id,{ext:u.ext})}
   else msgs(i).filter(m=>/^(human|user)$/i.test(m.role)).slice(-1).forEach(m=>mk('human',m.content,root.id,{ext:m.ext,imgs:m.imgs}));
   walk(root);const a=o&&(o.answer??o.output);if(a)mk('ai',typeof a==='string'?a:jtxt(a),root.id);else if(root.error)mk('system',root.error,root.id,{err:true})})}
 M.forEach((m,i)=>{const p=M[i-1];if(m.hist&&m.role==='tool'&&p&&p.hist&&p.role==='tool'&&p.run===m.run)m.grp=p.grp=p.grp||'g'+m.run+':'+i});   // consecutive tool results of ONE model call = parallel calls
 return{M,subs,turns:roots.map(t=>({id:t.id,root:t,st:turnStats(t,runs.filter(x=>rootOf(x)===t))}))}}
// whole thread: turns in time order; a turn whose messages start with the WHOLE previous history gets that repeated part dropped, and a turn left empty disappears
function mergeThread(parts){const key=m=>(/^(human|user)$/i.test(m.role)?'u':/^(ai|assistant)$/i.test(m.role)?'a':m.role)+'\u0001'+m.content,M=[],turns=[],subs={};let hist=[];
 parts.forEach(p=>{let i=0;while(i<hist.length&&i<p.M.length&&key(p.M[i])===hist[i])i++;const c0=hist.length&&i===hist.length?i:0,rest=p.M.slice(c0);
  hist=c0?p.M.map(key):hist.concat(rest.map(key));const has=new Set(rest.map(m=>m.turn));M.push(...rest);turns.push(...p.turns.filter(t=>has.has(t.id)));Object.assign(subs,p.subs)});
 return{M,turns,subs}}
//>>pure
// prepTrace: index runs + build the two views' data once per opened trace
function prepTrace(){const runs=CUR.runs,by={};runs.forEach(r=>by[r.id]=r);const isRoot=r=>!by[r.parent_id]||!!(r.metadata&&r.metadata.resumed_from);
 const rootOf=r=>{let p=r,n=0;while(!isRoot(p)&&n++<50)p=by[p.parent_id];return p},dep=r=>{let p=r,d=0;while(!isRoot(p)&&d<50){p=by[p.parent_id];d++}return d};
 const t0=Math.min(...runs.map(r=>new Date(r.start_time).getTime())),resumed=runs.some(r=>r.metadata&&r.metadata.resumed_from);
 runs.forEach((r,i)=>{r._k=i;r._hay=hay(r.name,r.model,r.error,r.inputs,r.outputs)});
 CUR.steps=runs.map((r,i)=>{const o=pj(r.outputs),inp=pj(r.inputs),md=r.metadata||{};let kind='other',title=r.name,sub='',res='';
  if(r.run_type==='tool'){kind='tool';sub=argSum(inp);res=cut(jtxt(o&&o.result!==undefined?o.result:o),260)}
  else if(r.run_type==='llm'){kind='model';title=r.model||r.name;const a=msgs(inp),b=msgs(o);sub=a.length?cut(a.slice(-1)[0].content,160):'';res=b.length?cut(b.slice(-1)[0].content,260):''}
  else if(md.resumed_from){kind='approval';title=T_('Resumed after approval','Tiếp tục sau khi duyệt');sub=cut(inp&&inp.message,160);res=cut(o&&o.answer,260)}
  else if(isRoot(r)){const ans=o&&(o.answer??o.output);if(resumed&&!ans&&!r.error){kind='interrupt';title=T_('Waiting for approval','Chờ duyệt')+' · '+r.name}else{kind='run';title=r.name;res=cut(ans,260)}sub=cut(uw(inp&&(inp.message||inp.question||jtxt(inp))),160)}
  else if(r.run_type==='chain'){kind='agent';sub=cut(inp&&(inp.message||inp.description||inp.input||jtxt(inp)),160);res=cut(o&&(o.answer||o.output||jtxt(o)),200)}
  else{sub=argSum(inp);res=cut(jtxt(o),200)}
  if(r.status==='error'){res=cut(r.error||res,260)}
  return{id:r.id,kind,title,sub,res,err:r.status==='error',depth:dep(r),off:(new Date(r.start_time).getTime()-t0)/1000,lat:r.latency_ms,tok:r.total_tokens,cost:r.cost_usd,hay:r._hay+' '+kind+' '+KIND_LBL()[kind].toLowerCase(),k:i}});
 CUR.m1=CUR.cur=buildMsgs(runs);CUR.mlist=CUR.cur.M;CUR.tm=null;MSCOPE='turn';CAPS={trajectory:CAP_STEP,messages:CAP_STEP}}
const more=(v,total)=>total>CAPS[v]?`<div style="text-align:center;margin:10px"><button class="g" data-more="${v}">${T_('Show more','Hiện thêm')} (${total-CAPS[v]})</button></div>`:'';
function renderTraj(b){const S=CUR.steps,KL=KIND_LBL();
 b.innerHTML=`<div style="flex:1;overflow:auto"><div class="tl">${truncNote()}<div class="row" style="margin-bottom:10px;font-size:12px;color:var(--t3)"><b style="color:var(--t1)">${T_('Trajectory','Hành trình')}</b><span>${S.length} ${T_('steps','bước')}</span><span>${S.filter(x=>x.kind==='model').length} ${T_('model calls','lần gọi model')}</span><span>${S.filter(x=>x.kind==='tool').length} ${T_('tool calls','lần gọi công cụ')}</span><span>${fmtS(CUR.root.latency_ms)}</span><span>${ICN('coins')} ${fmtN(S.reduce((a,x)=>a+(x.kind==='model'?x.tok||0:0),0)||CUR.root.total_tokens||0)} / $${Number(CUR.root.cost_usd||0).toFixed(6)}</span></div>
  ${S.slice(0,CAPS.trajectory).map(x=>`<div class="st" data-k="${x.k}" data-run="${x.id}" style="margin-left:${Math.min(x.depth,6)*18}px"><span class="dot k-${x.err?'error':x.kind}">${ICN(KIND_ICON[x.err?'error':x.kind])}</span><div class="sb">
   <div class="l1"><b translate="no">${hl(x.title)}</b><span class="chip">${KL[x.kind]}</span><span class="muted" style="font-size:11.5px">+${x.off.toFixed(2)}s</span>${latPill(x.lat)}${x.tok||x.cost?tokCost(x.tok,x.cost):''}</div>
   ${x.sub?`<div class="sub" translate="no">${hl(x.sub)}</div>`:''}${x.res?`<div class="res ${x.err?'err':''}" translate="no">→ ${hl(x.res)}</div>`:''}</div></div>`).join('')}${more('trajectory',S.length)}</div></div>`;
 b.onclick=e=>{const m=e.target.closest('[data-more]');if(m){CAPS[m.dataset.more]+=CAP_STEP;renderPane();return}const rn=e.target.closest('[data-run]');if(rn){PVIEW='details';SELR=rn.dataset.run;document.querySelectorAll('#pview button').forEach(x=>x.classList.toggle('on',x.dataset.v==='details'));renderPane()}}}
const ROLE2=()=>({human:T_('Human','Người dùng'),user:T_('Human','Người dùng'),ai:'AI',assistant:'AI',system:T_('System','Hệ thống'),tool:T_('Tool','Công cụ'),reasoning:T_('Reasoning','Suy luận')});
let MSCOPE='turn';const THREAD_CAP=20;
const ttftChip=st=>st&&st.ttft!=null?`<span class="chip" title="${esc(T_('Time to first token','Thời gian đến token đầu tiên'))}">${ICN('zap')}TTFT ${fmtS(st.ttft)}</span>`:'';
const truncNote=()=>CUR&&CUR.truncated?`<div class="tnote">${ICN('circle-alert')}${T_('Showing only the first 1000 runs of this trace','Chỉ hiện 1000 run đầu tiên của trace này')}</div>`:'';
function turnHead(t,n){if(!t)return'';const st=t.st;return `<div class="th2"><b>${T_('Turn','Lượt')} ${n}</b><small class="muted">${dt(t.root.start_time)}</small>${latPill(st.lat)}${tokCost(st.tokens,st.cost)}${ttftChip(st)}${st.tps?`<span class="chip" title="${esc(T_('Output tokens per second after the first token','Token đầu ra mỗi giây sau token đầu tiên'))}">${st.tps.toFixed(1)} tok/s</span>`:''}</div>`}
function msgCard(m,i){const R2=ROLE2(),rl=String(m.role).toLowerCase(),cls=rl==='user'?'human':rl==='assistant'||rl==='reasoning'?'ai':rl,lab=R2[rl]||m.role,q=sq_(),open=q&&m.hay.includes(q);let body;
 if(m.think)body=`<details ${open?'open':''}><summary>${ICN('sparkles')}<b>${T_('Reasoning','Suy luận')}</b><span class="muted" translate="no">${hl(cut(m.content,70))}</span></summary><div class="tx" translate="no">${hl(m.content)}</div></details>`;
 else if(m.retr)body=`<details open><summary>${ICN('layers')}<b translate="no">${hl(m.name||'retriever')}</b><span class="muted">${m.docs.length} ${T_('documents','tài liệu')}</span></summary>${m.docs.map(d=>`<div class="doc"><b translate="no">${hl(d.title)}</b><div class="tx" translate="no">${hl(d.text)}</div></div>`).join('')||`<div class="muted">${T_('No documents','Không có tài liệu')}</div>`}</details>`;
 else if(cls==='tool')body=`<details ${open?'open':''}><summary>${ICN('wrench')}<b translate="no">${hl(m.name||lab)}</b>${extBadge(m)}${m.args?`<span class="muted" translate="no">${hl(cut(m.args,60))}</span>`:''}<span class="muted">${T_('result','kết quả')}</span><span class="muted" translate="no">${hl(cut(m.content,70))}</span></summary>${m.args?`<div class="tx muted" translate="no">${hl(m.args)}</div>`:''}<div class="tx ${m.err?'err':''}" translate="no">${hl(m.content)}</div></details>`;
 else body=`<div class="rl">${lab}${extBadge(m)}</div><div class="tx ${m.err?'err':''}" translate="no">${hl(m.content)}</div>${imgsHtml(m.imgs)}`;
 return `<div class="mb ${cls}" data-k="${i}" data-run="${m.run}"><div class="bx">${body}</div></div>`}
// one turn's entries -> nested sub-agent blocks (collapsed) + parallel-tool rows
function renderList(items,subs){const q=sq_(),stack=[];let out='';const close=n=>{while(stack.length>n){out+='</div></details>';stack.pop()}};
 for(let k=0;k<items.length;){const{m,i}=items[k],path=m.path||[];let c=0;while(c<stack.length&&c<path.length&&stack[c]===path[c])c++;close(c);
  for(let j=c;j<path.length;j++){const id=path[j],sb=subs[id]||{},hit=q&&items.some(x=>(x.m.path||[]).includes(id)&&x.m.hay.includes(q));
   out+=`<details class="sa" ${hit?'open':''}><summary>${ICN('git-branch')}<b translate="no">${hl(sb.name||'sub-agent')}</b><span class="chip">${T_('Sub-agent','Agent con')}</span>${latPill(sb.lat)}${tokCost(sb.tokens,sb.cost)}</summary><div class="sab">`;stack.push(id)}
  if(m.grp){let e=k;while(e<items.length&&items[e].m.grp===m.grp&&(items[e].m.path||[]).join()===path.join())e++;
   if(e-k>1){out+=`<div class="pg"><div class="pgl">${ICN('zap')}${e-k} ${T_('tool calls in parallel','lệnh gọi công cụ chạy song song')}</div><div class="pgr">${items.slice(k,e).map(x=>msgCard(x.m,x.i)).join('')}</div></div>`;k=e;continue}}
  out+=msgCard(m,i);k++}
 close(0);return out}
function attHtml(){const A=CUR.att||[];if(!A.length)return'';const TXT=/^(text\/|application\/json)|\.(json|csv|txt|md|log)$/i,kb=n=>n==null?'':n<1024?n+' B':(n/1024).toFixed(1)+' KB';
 return `<div class="mat"><div class="h4">${ICN('paperclip')} ${T_('Attachments','Tệp đính kèm')} (${A.length})</div>${A.map(a=>{const u='api/obs/attachments/'+a.id,nm=esc(a.name),sz=`<small class="muted">${kb(a.size)}</small>`,mi=a.mime||'';
  if(/^image\/(png|jpe?g|gif|webp|bmp)$/.test(mi)&&(a.size||0)<=4194304)return `<div class="att"><b>${nm}</b> ${sz}<img class="thumb" src="${u}" alt="" loading="lazy"></div>`;
  if((TXT.test(mi)||TXT.test(a.name||''))&&(a.size||0)<=102400)return `<details class="att" data-att="${a.id}"><summary>${ICN('file-text')}<b>${nm}</b> ${sz}</summary><pre class="attp"></pre></details>`;
  return `<div class="att">${ICN('file-text')}<b>${nm}</b> ${sz} <a href="${u}" download="${nm}">${ICN('download')}${T_('Download','Tải về')}</a></div>`}).join('')}</div>`}
async function loadThreadMsgs(){const th=CUR.thread.slice(-THREAD_CAP);const parts=await Promise.all(th.map(async t=>t.trace_id===CUR.tid?CUR.m1:buildMsgs((await api('api/obs/traces/'+t.trace_id)).runs)));CUR.tm=mergeThread(parts)}
function setScope(sc){MSCOPE=sc;CUR.cur=sc==='thread'&&CUR.tm?CUR.tm:CUR.m1;CUR.mlist=CUR.cur.M;CAPS.messages=CAP_STEP;renderPane()}
function renderMsgs(b){const L=CUR.mlist,R=CUR.cur,tn={};R.turns.forEach((t,n)=>tn[t.id]={t,n:n+1});
 const groups=[];L.slice(0,CAPS.messages).forEach((m,i)=>{let g=groups[groups.length-1];if(!g||g.t!==m.turn){g={t:m.turn,items:[]};groups.push(g)}g.items.push({m,i})});
 const multi=CUR.thread&&CUR.thread.length>1;
 b.innerHTML=`<div style="flex:1;overflow:auto"><div class="mw">${truncNote()}${multi?`<div class="row" style="margin-bottom:8px"><button class="g ${MSCOPE==='turn'?'on':''}" data-sc="turn">${T_('This turn','Lượt này')}</button><button class="g ${MSCOPE==='thread'?'on':''}" data-sc="thread">${ICN('messages-square')}${T_('Whole thread','Cả cuộc hội thoại')} (${CUR.thread.length})</button>${CUR.thread.length>THREAD_CAP&&MSCOPE==='thread'?`<small class="muted">${T_('last','chỉ')} ${THREAD_CAP} ${T_('turns','lượt cuối')}</small>`:''}</div>`:''}
  ${L.length?groups.map(g=>turnHead((tn[g.t]||{}).t,(tn[g.t]||{}).n)+renderList(g.items,R.subs)).join('')+more('messages',L.length):`<div class="empty"><b>${T_('No messages in this trace','Trace này không có tin nhắn')}</b></div>`}
  ${attHtml()}<div class="row" style="margin-top:16px;justify-content:center"><button class="g" id="mdl">${ICN('download')}${MSCOPE==='thread'?T_('Download thread as Markdown','Tải cuộc hội thoại dạng Markdown'):T_('Download as Markdown','Tải về dạng Markdown')}</button></div></div></div>`;
 b.onclick=async e=>{const m=e.target.closest('[data-more]');if(m){CAPS[m.dataset.more]+=CAP_STEP;renderPane();return}
  const sc=e.target.closest('[data-sc]');if(sc){if(sc.dataset.sc==='thread'&&!CUR.tm){try{flash(T_('Loading the whole thread…','Đang tải cả cuộc hội thoại…'));await loadThreadMsgs()}catch(er){flash(er.message,1);return}}setScope(sc.dataset.sc);return}
  if(e.target.closest('summary,details,a'))return;const rn=e.target.closest('[data-run]');if(rn&&!window.getSelection().toString()){PVIEW='details';SELR=rn.dataset.run;document.querySelectorAll('#pview button').forEach(x=>x.classList.toggle('on',x.dataset.v==='details'));renderPane()}};
 b.querySelectorAll('details[data-att]').forEach(d=>d.addEventListener('toggle',async()=>{if(!d.open||d.dataset.ld)return;d.dataset.ld='1';const p=d.querySelector('pre');try{const r=await fetch('api/obs/attachments/'+d.dataset.att,{credentials:'same-origin'});p.textContent=(await r.text()).slice(0,102400)}catch(er){p.textContent=String(er.message||er)}}));
 $('mdl').onclick=()=>{const R2=ROLE2(),md=R.turns.map((t,n)=>{const st=t.st,hd=`## ${T_('Turn','Lượt')} ${n+1} - ${[st.tokens?fmtN(st.tokens)+' tokens':'',st.cost?'$'+Number(st.cost).toFixed(6):'',st.lat!=null?fmtS(st.lat):'',st.ttft!=null?'TTFT '+fmtS(st.ttft):'',st.tps?st.tps.toFixed(1)+' tok/s':''].filter(Boolean).join(' | ')}\n`;
   return hd+'\n'+L.filter(m=>m.turn===t.id).map(m=>{const sa=m.path&&m.path.length?' ['+T_('sub-agent','agent con')+': '+((R.subs[m.path[m.path.length-1]]||{}).name||'')+']':'';
    return `**${R2[String(m.role).toLowerCase()]||m.role}${m.name?' ('+m.name+')':''}${m.ext?' ['+T_('external','nguồn ngoài')+']':''}${sa}:** ${m.retr?'\n'+m.docs.map(d=>'- '+d.title+': '+d.text).join('\n'):m.content}\n`}).join('\n')}).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown'}));a.download=MSCOPE==='thread'&&CUR.root.thread_id?'thread-'+String(CUR.root.thread_id).replace(/[^\w.-]/g,'_').slice(0,40)+'.md':'trace-messages.md';a.click()}}
// ---- search inside a trace (Ctrl/Cmd+F): highlights matches across Trajectory, Messages, Turns and the Details tree (tab badges show counts); next/prev jumps
function viewItems(){if(!CUR)return[];if(PVIEW==='trajectory')return CUR.steps;if(PVIEW==='messages')return CUR.mlist;
 if(PVIEW==='turns')return(CUR.thread&&CUR.thread.length?CUR.thread:[CUR.root]).map(t=>({hay:hay(t.inputs,t.outputs,t.error)}));return CUR.runs}
function searchApply(){if(!CUR)return;const q=sq_(),it=viewItems();SMATCH=q?it.map((x,i)=>(x.hay||x._hay).includes(q)?i:-1).filter(i=>i>=0):[];if(SI>=SMATCH.length)SI=0;
 const cap=CAPS[PVIEW];if(q&&cap&&SMATCH.length&&SMATCH[SI]>=cap){CAPS[PVIEW]=Math.ceil((SMATCH[SI]+1)/CAP_STEP)*CAP_STEP;return renderPane()}
 $('scount').textContent=q?(SMATCH.length?`${SI+1}/${SMATCH.length}`:T_('No match','Không thấy')):'';
 document.querySelectorAll('#pview button').forEach(x=>{const v=x.dataset.v,l=v==='trajectory'?CUR.steps:v==='messages'?CUR.mlist:v==='details'?CUR.runs:null;if(q&&l)x.dataset.n=l.filter(y=>(y.hay||y._hay).includes(q)).length;else delete x.dataset.n});
 const hs=new Set(SMATCH);document.querySelectorAll('#pbody [data-k]').forEach(e=>{e.classList.toggle('hit',hs.has(+e.dataset.k));e.classList.remove('cur')});
 if(q&&SMATCH.length){const cur=document.querySelector(`#pbody [data-k="${SMATCH[SI]}"]`);if(cur){cur.classList.add('cur');cur.scrollIntoView({block:'center'})}}}
function searchGo(d){if(!SMATCH.length)return;SI=(SI+d+SMATCH.length)%SMATCH.length;
 if(PVIEW==='details'){const r=CUR.runs[SMATCH[SI]];SELR=r.id;document.querySelectorAll('#pbody .tn').forEach(x=>x.classList.toggle('on',x.dataset.id===SELR));renderRun()}searchApply()}
let SQT=null;$('sq').oninput=e=>{SQ=e.target.value;SI=0;clearTimeout(SQT);SQT=setTimeout(()=>CUR&&renderPane(),150)};
$('sq').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();searchGo(e.shiftKey?-1:1)}else if(e.key==='Escape'){e.stopPropagation();SQ='';e.target.value='';e.target.blur();CUR&&renderPane()}};
$('snext').onclick=()=>searchGo(1);$('sprev').onclick=()=>searchGo(-1);
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='f'&&$('pane').classList.contains('on')){e.preventDefault();$('sq').focus();$('sq').select()}});
function renderTurns(b){const turns=CUR.thread&&CUR.thread.length?CUR.thread:[CUR.root];
 b.innerHTML=`<div style="flex:1;overflow:auto;padding:6px 0">${turns.map((t,i)=>`<div class="turn" data-k="${i}"><div class="row"><b>Turn ${i+1}</b><small class="muted">${dt(t.start_time)}</small>${latPill(t.latency_ms)}${tokCost(t.total_tokens,t.cost_usd)}<span class="sp"></span><a href="#" data-tr="${t.trace_id||CUR.tid}">Open trace</a></div>
  <div class="grid2" style="margin-top:6px"><div><div class="h4">Input</div>${bubbles(msgs(t.inputs))||`<pre>${esc(JSON.stringify(t.inputs,null,1))}</pre>`}</div><div><div class="h4">Output</div>${t.error?`<pre class="err">${esc(t.error)}</pre>`:bubbles(msgs(t.outputs))||`<pre>${esc(JSON.stringify(t.outputs,null,1))}</pre>`}</div></div></div>`).join('')}</div>`;
 b.onclick=e=>{const tr=e.target.closest('[data-tr]');if(tr){e.preventDefault();PVIEW='details';openTrace(tr.dataset.tr)}}}
// ---- project tabs
document.querySelectorAll('#ptabs button').forEach(b=>b.onclick=()=>ptab(b.dataset.t));
async function ptab(t){PTAB=t;document.querySelectorAll('#ptabs button').forEach(x=>x.classList.toggle('on',x.dataset.t===t));$('tab_tracing').style.display=t==='tracing'?'flex':'none';const o=$('tab_other');o.style.display=t==='tracing'?'none':'block';if(t==='tracing')return;
 o.innerHTML=`<div class="muted">${ICN('loader')} Loading…</div>`;
 if(t==='engine'){const d=await api(`api/obs/projects/${PROJ.id}/errors?hours=168&mode=signature`);o.innerHTML=`<div class="phead"><h2 style="margin:0">Issues</h2><span class="chip">${d.total_errors} error runs · ${d.clusters.length} issues · last 7 days</span></div>
  <div class="card" style="padding:0;margin-top:10px"><table><thead><tr><th>Severity</th><th>Issue</th><th>Occurrences</th><th>Traces</th><th>Runs</th><th>First seen</th><th>Last seen</th><th>Evidence</th></tr></thead><tbody>${d.clusters.map(c=>`<tr><td><span class="chip pill ${c.count>=5?'er':'wn'}">${c.count>=5?'High':'Medium'}</span></td><td class="mono" style="font-size:12px;max-width:420px">${esc(c.signature)}</td><td>${c.count}</td><td>${c.n_traces}</td><td>${c.names.map(([n,k])=>`<span class="chip">${esc(n)} ×${k}</span>`).join('')}</td><td>${dt(c.first)}</td><td>${dt(c.last)}</td><td><a href="#" onclick="openTrace('${c.sample_trace}');return false">View trace</a></td></tr>`).join('')||`<tr><td colspan="8"><div class="empty"><span class="tile">${ICN('circle-check')}</span><b>No active issues</b>Engine found no recurring errors in the last 7 days.</div></td></tr>`}</tbody></table></div>`}
 else if(t==='automations'){const r=(await api('api/obs/rules')).filter(x=>x.project_id===PROJ.id||x.project===PROJ.name);o.innerHTML=`<div class="phead"><h2 style="margin:0">Automations</h2><span class="sp"></span><a class="btn" href="rules">${ICN('plus')}New automation</a></div>
  <div class="card" style="padding:0;margin-top:10px"><table><thead><tr><th>Name</th><th>Filter</th><th>Sampling rate</th><th>Actions</th><th>Matched / Applied</th><th>Enabled</th><th></th></tr></thead><tbody>${r.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td class="mono" style="font-size:12px">${esc(x.filter||'All runs')}</td><td>${x.sampling_rate}</td><td>${(x.actions||[]).map(a=>`<span class="chip">${esc(a.type)}</span>`).join('')}</td><td>${x.n_matched} / ${x.n_applied}</td><td>${x.enabled?'<span class="chip pill ok">On</span>':'<span class="chip pill">Off</span>'}</td><td><a href="rules">Logs</a></td></tr>`).join('')||`<tr><td colspan="7"><div class="empty"><span class="tile">${ICN('zap')}</span><b>No automations</b>Automations apply actions to runs that match a filter.</div></td></tr>`}</tbody></table></div>`}
 else if(t==='evaluators'){const ev=await api('api/evaluators');o.innerHTML=`<div class="phead"><h2 style="margin:0">Online Evaluators</h2><span class="sp"></span><a class="btn" href="evaluators">${ICN('plus')}New evaluator</a></div>
  <div class="card" style="padding:0;margin-top:10px"><table><thead><tr><th>Name</th><th>Type</th><th>Feedback key</th></tr></thead><tbody>${ev.map(e=>`<tr><td><b>${esc(e.name)}</b></td><td>${e.kind==='llm'?'LLM-as-a-Judge':e.kind==='code'?'Code':esc(e.kind)}</td><td class="mono">${esc(e.config.key||'')}</td></tr>`).join('')}</tbody></table></div>`}
 else{const d=await api(`api/obs/projects/${PROJ.id}/dashboard?hours=168&bucket=day`);const t0=d.total;o.innerHTML=`<div class="phead"><h2 style="margin:0">Insights</h2><span class="chip">Last 7 days</span></div>
  <div class="kpi" style="margin-top:10px"><div>Traces<b>${fmtN(t0.n)}</b></div><div>Error rate<b>${t0.n?(t0.errors/t0.n*100).toFixed(1):0}%</b></div><div>P50 latency<b>${fmtS(t0.p50)}</b></div><div>Tokens<b>${fmtN(t0.tokens)}</b></div><div>Cost<b>$${Number(t0.cost||0).toFixed(4)}</b></div></div>
  <div class="card" style="margin-top:12px"><div class="h4">Top tools</div><table><thead><tr><th>Tool</th><th>Calls</th><th>Errors</th><th>P50</th></tr></thead><tbody>${d.tools.map(x=>`<tr><td>${esc(x.name)}</td><td>${x.calls}</td><td>${x.errors}</td><td>${latPill(x.p50)}</td></tr>`).join('')}</tbody></table></div>`}}
// ---- project selection / landing
const envBadge=e=>`<span class="env env-${esc(e||'prod')}" title="${esc(T_('Environment','Môi trường'))}">${esc(e||'prod')}</span>`;
const envOf=p=>envBadge(p.environment);
async function landing(){$('landing').style.display='block';$('proj').style.display='none';
 const fe=$('penv').value;
 $('penv').onchange=landing;
 $('pl').innerHTML=PROJS.filter(p=>!fe||p.environment===fe).map(p=>`<tr class="r" onclick="location.href='projects?p=${p.id}'"><td><div class="nm">${rtTile('chain')}${esc(p.name)}</div><div class="muted" style="font-size:12px">${esc(p.description||'')}</div></td><td>${envOf(p)}</td><td>${fmtN(p.traces_24h)}</td><td>${errPill(p.traces_24h?p.errors_24h/p.traces_24h:0)}</td><td>${fmtN(p.traces)}</td><td><span class="chip">${p.retention_days}d</span></td><td>${rel(p.last_run)}</td></tr>`).join('')}
$('npb').onclick=async()=>{try{const p=await api('api/obs/projects','POST',{name:$('np').value,environment:$('npe').value});location.href='projects?p='+p.id}catch(e){flash(e.message,1)}};
function openProject(p){PROJ=p;$('landing').style.display='none';$('proj').style.display='flex';$('pt').textContent=p.name;$('pid').innerHTML=idChip(p.id)+' <a href="#" id="penvb" title="'+esc(T_('Change environment','Đổi môi trường'))+'">'+envBadge(p.environment)+'</a>';
 $('penvb').onclick=async e=>{e.preventDefault();const v=prompt(T_('Environment (dev, staging, prod or a custom label)','Môi trường (dev, staging, prod hoặc nhãn tự đặt)'),p.environment);if(!v)return;try{await api('api/obs/projects/'+p.id,'PATCH',{environment:v});location.reload()}catch(er){flash(er.message,1)}};$('pret').textContent='Retention '+p.retention_days+'d';
 $('pdash').href='monitoring?p='+p.id;$('psel').innerHTML=PROJS.map(x=>`<option value="${x.id}" ${x.id===p.id?'selected':''}>${esc(x.name)}</option>`).join('');load()}
$('psel').onchange=e=>location.href='projects?p='+e.target.value;
$('pset').onclick=async()=>{const v=prompt('Retention (days)',PROJ.retention_days);if(!v)return;await api('api/obs/projects/'+PROJ.id,'PATCH',{retention_days:+v});PROJ.retention_days=+v;$('pret').textContent='Retention '+v+'d';flash('Saved')};
$('pnew').onclick=e=>{aosPopup(e.currentTarget,`<a class="mi" href="rules">${ICN('zap')}New automation</a><a class="mi" href="evaluators">${ICN('gauge')}New evaluator</a><a class="mi" href="rules">${ICN('bell')}New alert</a>`,{right:true})};
(async()=>{[PROJS,QUEUES,DSETS]=await Promise.all([api('api/obs/projects'),api('api/queues').catch(()=>[]),api('api/datasets').catch(()=>[])]);await loadViews();
 const want=QS.get('p');const p=PROJS.find(x=>String(x.id)===want);const AGQ=QS.get('agent');if(QS.get('filter')){RAW=QS.get('filter');setTimeout(()=>{$('rawb')&&$('rawb').click()},0)}if(+QS.get('since')){SINCE=+QS.get('since');const tn=$('tname');if(tn)tn.textContent=(TIMES.find(t=>t[0]===SINCE)||[0,SINCE+'h'])[1]}if(AGQ&&/^\d+$/.test(AGQ)){CHIPS=[{attr:'metadata',key:'agent_id',op:'eq',value:AGQ}];setTimeout(()=>drawChips(),0)}if(!p&&!AGQ&&!QS.get('trace')&&!QS.get('tab'))return landing();openProject(p||PROJS[0]);if(QS.get('tab'))ptab(QS.get('tab'));
 if(QS.get('trace'))openTrace(QS.get('trace'),QS.get('run')||null).catch(e=>flash(e.message,1))})().catch(e=>flash(e.message,1));
"""

TRACING = page("projects", "Tracing", BODY, JS)
PAGES = {"/projects": TRACING}
