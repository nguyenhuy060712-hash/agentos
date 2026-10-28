"""i18n: MỘT từ điển duy nhất (i18n_dict.json: chuỗi nguồn tiếng Việt -> {"en", "vi"(bản đã hiệu đính, tuỳ chọn)}).
Giao diện: mọi chuỗi hiển thị (text node, placeholder, title, alert/confirm/prompt, nội dung thêm động) đi qua bộ dịch JS dùng từ điển này.
Mặc định tiếng Anh; chọn EN/VI lưu theo người dùng (users.lang) + cookie aos_lang (cho trang đăng nhập).
API: thông báo lỗi luôn tiếng Anh (dịch detail qua cùng từ điển)."""
import json, re
from pathlib import Path
DICT: dict = json.loads((Path(__file__).parent / "i18n_dict.json").read_text(encoding="utf-8"))
_KEYS = sorted([k for k in DICT if re.search(r"\s", k.strip()) or len(k) >= 12 or re.search(r"[$#:]", k)], key=len, reverse=True)
_RX = re.compile(r"(?<![^\W\d_])(" + "|".join(re.escape(k) for k in _KEYS) + r")(?![^\W\d_])")

def en(s):
    """Dịch chuỗi (có thể chứa giá trị chèn) sang tiếng Anh theo từ điển."""
    if not isinstance(s, str) or not s: return s
    z = s.strip()
    if z in DICT: return s.replace(z, DICT[z]["en"])
    return _RX.sub(lambda m: DICT[m.group(1)]["en"], s)

def en_detail(d):
    if isinstance(d, str): return en(d)
    if isinstance(d, dict): return {k: en_detail(v) for k, v in d.items()}
    if isinstance(d, list): return [en_detail(x) for x in d]
    return d

_EN = {k: v["en"] for k, v in DICT.items() if v.get("src") != "en"}   # mục nguồn tiếng Anh: giữ nguyên ở EN
_VI = {k: v["vi"] for k, v in DICT.items() if v.get("vi")}
# Server-side replacement is limited to keys that cannot collide with page code:
#  EN mode: Vietnamese-source keys containing non-ASCII letters (JS identifiers/keywords are ASCII).
#  VI mode: English keys with at least one space (never a JS identifier). Everything else (single words, dynamic text,
#  toasts, attributes) is translated in the browser by key lookup on the rendered DOM (see CLIENT_JS), never on code.
_SAFE_EN = {k: v for k, v in _EN.items() if re.search(r"[^\x00-\x7f]", k)}
_SAFE_VI = {k: v for k, v in _VI.items() if v.get("src") == "en" and " " in k.strip()} if False else {k: DICT[k]["vi"] for k in _VI if DICT[k].get("src") == "en" and " " in k.strip()}
def _rx(keys): return re.compile(r"(?<![^\W\d_])(" + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + r")(?![^\W\d_])") if keys else None
_RX_EN, _RX_VI = _rx(_SAFE_EN), _rx(_SAFE_VI)
_ALLRX = _rx(DICT)
# Client dictionaries: exact-string lookup tables
CLIENT_VI = {}
for k, v in DICT.items():
    if v.get("src") == "en":
        if v.get("vi"): CLIENT_VI[k] = v["vi"]
    else:
        if v.get("en"): CLIENT_VI[v["en"]] = v.get("vi") or k
CLIENT_EN = {}
for k, v in DICT.items():
    if v.get("src") != "en" and v.get("en"): CLIENT_EN[k] = v["en"]
    if v.get("vi") and v.get("en") and v["vi"] != v["en"] and (DICT.get(v["vi"]) or {}).get("src") != "en": CLIENT_EN[v["vi"]] = v["en"]   # 8.P: "Model" (en key) must not become "Models" just because Models' vi is "Model"
_CACHE: dict = {}

def _safe(x):   # bản dịch chèn vào literal JS/HTML: không được làm vỡ dấu nháy
    return x.replace("'", "’").replace('"', "”").replace("`", "’").replace("${", "$ {").replace("<", "‹").replace(">", "›")

def localize(html: str, lang: str = "en", cache: bool = True) -> str:
    """Dịch MÃ NGUỒN trang (HTML + literal JS) theo từ điển ⇒ dữ liệu người dùng tải động không bao giờ bị dịch nhầm."""
    lang = lang if lang in ("en", "vi") else "en"
    key = (id(html), len(html), lang)
    if cache and key in _CACHE: return _CACHE[key]
    m, rx = (_SAFE_EN, _RX_EN) if lang == "en" else (_SAFE_VI, _RX_VI)
    out = rx.sub(lambda g: _safe(m[g.group(1)]), html) if rx else html
    dj = json.dumps(CLIENT_EN if lang == "en" else CLIENT_VI, ensure_ascii=False).replace("</", "<\\/")
    out = out.replace("/*AOS_DICT*/", "window.AOS_DICT=" + dj + ";", 1)
    out = out.replace('<html lang="vi">', f'<html lang="{lang}">').replace("<html lang=vi>", f"<html lang={lang}>")
    if cache: _CACHE[key] = out
    return out

def lang_of(request) -> str:
    v = request.cookies.get("aos_lang") if request is not None else None
    return v if v in ("en", "vi") else "en"

JS = r"""
(function(){
const ck=n=>(document.cookie.match('(?:^|; )'+n+'=([^;]*)')||[])[1];
window.LANG=ck('aos_lang')||'en';/*AOS_DICT*/const D=window.AOS_DICT||{};
const PAT=Object.keys(D).filter(k=>/\{[nx]\}/.test(k)).sort((a,b)=>b.length-a.length).map(k=>{const ph=[];const src='^'+k.replace(/[.*+?^$()|[\]\\]/g,'\\$&').replace(/\{([nx])\}/g,(m,c)=>{ph.push(c);return c==='n'?'(\\$?[\\d.,]+[KMB%]?)':'(.+?)'})+'$';return [new RegExp(src),D[k],ph]});
function t(s){if(typeof s!=='string'||!s)return s;const m=s.match(/^(\s*)([\s\S]*?)(\s*)$/);const core=m[2];if(!core)return s;let r=D[core];
 if(r===undefined){const mm=core.match(/^(.*?)([:：…]|\.\.\.)$/);if(mm&&D[mm[1]]!==undefined)r=D[mm[1]]+mm[2]}
 if(r===undefined){for(const [rx,v,ph] of PAT){const x=core.match(rx);if(x){let i=0;r=v.replace(/\{([nx])\}/g,(m,c)=>{const g=x[1+i++]||'';return c==='x'?t(g):g});break}}}
 return r===undefined?s:m[1]+r+m[3]}
window.t=t;const SKIP='script,style,pre,code,textarea,.mono,[translate=no],[data-noi18n]';
function tr(root){if(!root)return;if(root.nodeType===3){const p=root.parentElement;if(p&&!p.closest(SKIP)){const v=t(root.nodeValue);if(v!==root.nodeValue)root.nodeValue=v}return}
 if(root.nodeType!==1||root.closest&&root.closest(SKIP))return;
 const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;const L=[];while(n=w.nextNode())L.push(n);L.forEach(x=>tr(x));
 const els=[root,...root.querySelectorAll('[placeholder],[title],[aria-label],[data-i18n],input[type=button],input[type=submit]')];
 for(const e of els){if(!e.getAttribute||e.closest(SKIP))continue;for(const a of ['placeholder','title','aria-label']){const v=e.getAttribute(a);if(v){const x=t(v);if(x!==v)e.setAttribute(a,x)}}
  const k=e.getAttribute('data-i18n');if(k&&D[k]!==undefined)e.textContent=D[k];if(e.tagName==='INPUT'&&(e.type==='button'||e.type==='submit')&&e.value)e.value=t(e.value)}}
window.aosTr=tr;
for(const f of ['alert','confirm','prompt']){const o=window[f].bind(window);window[f]=(m,...a)=>o(t(m),...a)}
const mo=new MutationObserver(ms=>{for(const m of ms){if(m.type==='characterData')tr(m.target);else{m.addedNodes.forEach(tr);if(m.type==='attributes')tr(m.target)}}});
function boot(){tr(document.body);mo.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['placeholder','title','aria-label']});document.title=t(document.title.split(' · ')[0])+(document.title.includes(' · ')?' · '+document.title.split(' · ').slice(1).join(' · '):'')}
if(document.body)boot();else document.addEventListener('DOMContentLoaded',boot);
function setCookie(l){document.cookie='aos_lang='+l+';path=/;max-age=31536000;samesite=lax'+(location.protocol==='https:'?';secure':'')}
async function setLang(l){setCookie(l);try{await fetch('api/auth/lang',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:l})})}catch(x){}location.reload()}
window.aosSetLang=setLang;
function start(){let box=document.getElementById('langx');
 if(!box){box=document.createElement('span');box.id='langx';box.style.cssText='position:fixed;top:12px;right:14px;z-index:9';document.body.appendChild(box)}
 box.style.fontSize='12px';box.title=LANG==='vi'?'Ngôn ngữ':'Language';
 box.innerHTML='<span class="seg">'+['en','vi'].map(l=>`<button type="button" data-l="${l}" class="${l===LANG?'on':''}" style="height:22px;padding:0 8px;font-size:11px">${l.toUpperCase()}</button>`).join('')+'</span>';
 box.onclick=e=>{const b=e.target.closest('[data-l]');if(b&&b.dataset.l!==LANG)setLang(b.dataset.l)};
 if(!/\/login/.test(location.pathname))fetch('api/auth/me').then(r=>r.ok?r.json():null).then(m=>{if(m&&m.lang&&m.lang!==LANG&&!sessionStorage.getItem('aos_lang_sync')){
   sessionStorage.setItem('aos_lang_sync','1');setCookie(m.lang);location.reload()}}).catch(()=>{})}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
"""
TAG = "<script>" + JS + "</script>"

def inject(html: str) -> str:
    return html.replace("</head>", TAG + "</head>", 1) if "</head>" in html else TAG + html

def page_for(html: str, request) -> str:
    return localize(html, lang_of(request))
