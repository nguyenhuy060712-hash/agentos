"""GĐ12.81: fake Google Docs v1 + more of Sheets v4 for the fake workspace server (tests/fake_workspace_api.py loads this through EXTRA_ROUTES).
Docs:   GET /v1/documents/{id} · POST /v1/documents · POST /v1/documents/{id}:batchUpdate (insertText at an index or at the end, replaceAllText, deleteContentRange).
Sheets: GET /v4/spreadsheets/{id} · POST /v4/spreadsheets · POST /v4/spreadsheets/{id}:batchUpdate (addSheet) · GET values:batchGet · POST values/{range}:append.
        The old routes GET / PUT .../values/{range} of the main file ask this module first (`hook_sheet_get` / `hook_sheet_put`); an id this module does not know keeps the old answer.
Every document / spreadsheet has ONE owner (alice or bob): the other person's token gets 404, like Google's "not found" for something you cannot open. Each person starts with one of each
(`d-alice`, `d-bob`, `sh-alice`, `sh-bob`). Scope check like the other routes (only enforced when S["enforce_scopes"] is on). Calls are recorded: ST["batches"] (every batchUpdate request list),
ST["appends"] (every successful append) so a test can prove a retry wrote once."""
import re, secrets, urllib.parse
from aiohttp import web

GA = "https://www.googleapis.com/auth/"
DOCS_R, DOCS_W = GA + "documents.readonly", GA + "documents"
SH_R, SH_W = GA + "spreadsheets.readonly", GA + "spreadsheets"
ST = {"docs": {}, "sheets": {}, "batches": [], "appends": [], "seq": 0}
_H = {}   # helpers from the main module (_who, _err)

def reset(S):
    for k in ("docs", "sheets", "batches", "appends"): ST[k].clear()
    ST["seq"] = 0

def _nid(p): ST["seq"] += 1; return f"{p}-{ST['seq']}-{secrets.token_hex(2)}"

def _seed():
    if ST["docs"] or ST["sheets"]: return
    ST["docs"]["d-alice"] = {"id": "d-alice", "owner": "alice", "title": "Biên bản họp ALICE", "text": "Biên bản họp tháng 10\nDoanh thu đạt 1,2 tỷ đồng.\nViệc cần làm: gửi báo giá cho khách Lan.\n",
                            "styles": {"Biên bản họp tháng 10": "HEADING_1"}}
    ST["docs"]["d-bob"] = {"id": "d-bob", "owner": "bob", "title": "BOB-SECRET kế hoạch", "text": "BOB-SECRET kế hoạch quý 4\nChỉ Bob được đọc.\n", "styles": {"BOB-SECRET kế hoạch quý 4": "HEADING_1"}}
    ST["sheets"]["sh-alice"] = {"id": "sh-alice", "owner": "alice", "title": "Tồn kho ALICE", "tabs": [{"title": "Sheet1", "rows": [["Sản phẩm", "Số lượng"], ["Áo thun", "120"], ["Quần jean", "45"]]}]}
    ST["sheets"]["sh-bob"] = {"id": "sh-bob", "owner": "bob", "title": "BOB-SECRET", "tabs": [{"title": "Sheet1", "rows": [["BOB-SECRET", "1"]]}]}

def _err(status, message, st=None):
    return web.json_response({"error": {"code": status, "message": message, "status": st or ("NOT_FOUND" if status == 404 else "INVALID_ARGUMENT")}}, status=status)

# ------------------------------------------------------------------ Docs
def _render(d) -> dict:
    """Document resource: one paragraph per line, with the character indexes Google uses (the body starts at index 1)."""
    content, i = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}], 1
    for line in d["text"].split("\n")[:-1]:
        n = len(line) + 1
        content.append({"startIndex": i, "endIndex": i + n, "paragraph": {"elements": [{"startIndex": i, "endIndex": i + n, "textRun": {"content": line + "\n"}}],
                                                                      "paragraphStyle": {"namedStyleType": d["styles"].get(line, "NORMAL_TEXT")}}})
        i += n
    return {"documentId": d["id"], "title": d["title"], "body": {"content": content}, "revisionId": "r1"}

def _doc(req, who, *, write=False):
    _seed(); d = ST["docs"].get(req.match_info["id"].split(":")[0])
    if not d or d["owner"] != who: return None, _err(404, "Requested entity was not found.")
    return d, None

async def doc_get(req):
    who = _H["who"](req, "google", DOCS_R, DOCS_W); d, e = _doc(req, who)
    return e or web.json_response(_render(d))

async def doc_create(req):
    who = _H["who"](req, "google", DOCS_W); _seed(); b = await req.json()
    d = {"id": _nid("doc"), "owner": who, "title": str(b.get("title") or "Untitled document"), "text": "\n", "styles": {}}
    ST["docs"][d["id"]] = d
    return web.json_response(_render(d))

async def doc_batch(req):
    who = _H["who"](req, "google", DOCS_W); d, e = _doc(req, who)
    if e: return e
    b = await req.json(); reqs = b.get("requests") or []; ST["batches"].append({"who": who, "doc": d["id"], "requests": reqs}); replies, text = [], d["text"]
    for r in reqs:
        if "insertText" in r:
            it = r["insertText"]; s = str(it.get("text", ""))
            if "endOfSegmentLocation" in it: at = len(text) - 1
            else:
                idx = (it.get("location") or {}).get("index")
                if not isinstance(idx, int) or idx < 1 or idx > len(text): return _err(400, f"Invalid requests[0].insertText: Index {idx} must be less than the end index of the referenced segment, {len(text) + 1}.")
                at = idx - 1
            text = text[:at] + s + text[at:]; replies.append({})
        elif "replaceAllText" in r:
            ra = r["replaceAllText"]; ct = ra.get("containsText") or {}; find = str(ct.get("text", ""))
            if not find: return _err(400, "Invalid requests: replaceAllText needs containsText.text")
            flags = 0 if ct.get("matchCase") else re.I
            text, n = re.subn(re.escape(find), lambda _m: str(ra.get("replaceText", "")), text, flags=flags); replies.append({"replaceAllText": {"occurrencesChanged": n}})
        elif "deleteContentRange" in r:
            rg = (r["deleteContentRange"] or {}).get("range") or {}; s0, e0 = rg.get("startIndex"), rg.get("endIndex")
            if not (isinstance(s0, int) and isinstance(e0, int)) or s0 < 1 or e0 <= s0 or e0 > len(text) + 1: return _err(400, "Invalid requests: deleteContentRange has a bad range.")
            if e0 == len(text) + 1: e0 -= 1   # the last newline of a document cannot be deleted
            text = text[:s0 - 1] + text[e0 - 1:]; replies.append({})
        else: return _err(400, "Invalid requests: unsupported request " + ",".join(r))
    if not text.endswith("\n"): text += "\n"
    d["text"] = text
    return web.json_response({"documentId": d["id"], "replies": replies, "writeControl": {"requiredRevisionId": "r1"}})

# ------------------------------------------------------------------ Sheets
def _sheet(req, who):
    _seed(); s = ST["sheets"].get(req.match_info["sid"].split(":")[0])
    return (s, None) if s and s["owner"] == who else (None, _err(404, "Requested entity was not found."))

def _col(n):
    s = ""
    while n: n, r = divmod(n - 1, 26); s = chr(65 + r) + s
    return s

def _cn(letters):
    n = 0
    for ch in letters.upper(): n = n * 26 + ord(ch) - 64
    return n

def parse_range(s, sh):
    """'Sheet1!A1:B3' | 'Sheet1' | 'A1:C5' -> (tab, r1, c1, r2, c2) 1-based, None bounds = open. Unknown tab -> ValueError."""
    s = urllib.parse.unquote(s or ""); tab, _, rng = s.rpartition("!") if "!" in s else ("", "", s)
    tab = tab.strip("'")
    if not tab and rng and not re.fullmatch(r"\$?[A-Za-z]{1,3}\$?\d*(:\$?[A-Za-z]{1,3}\$?\d*)?", rng): tab, rng = rng.strip("'"), ""
    t = next((t for t in sh["tabs"] if t["title"] == tab), None) if tab else sh["tabs"][0]
    if t is None: raise ValueError(f"Unable to parse range: {s}")
    if not rng: return t, 1, 1, None, None
    m = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d*)(?::\$?([A-Za-z]{1,3})\$?(\d*))?", rng)
    if not m: raise ValueError(f"Unable to parse range: {s}")
    c1, r1, c2, r2 = m.groups()
    return t, int(r1 or 1), _cn(c1), (int(r2) if r2 else (int(r1) if r1 and not c2 else None)), (_cn(c2) if c2 else (_cn(c1) if not c2 else None))

def _slice(t, r1, c1, r2, c2):
    rows = t["rows"][r1 - 1:r2] if r2 else t["rows"][r1 - 1:]
    out = [list(r[c1 - 1:c2] if c2 else r[c1 - 1:]) for r in rows]
    out = [r for r in out]
    while out and not any(str(x) != "" for x in out[-1]): out.pop()
    for r in out:
        while r and str(r[-1]) == "": r.pop()
    return out

def _values(sh, rng):
    t, r1, c1, r2, c2 = parse_range(rng, sh); v = _slice(t, r1, c1, r2, c2)
    last_r = (r2 or (r1 + max(len(v), 1) - 1)); last_c = c2 or (c1 + max((len(x) for x in v), default=1) - 1)
    return {"range": f"{t['title']}!{_col(c1)}{r1}:{_col(last_c)}{last_r}", "majorDimension": "ROWS", **({"values": v} if v else {})}

def _cell(x): return x if isinstance(x, str) else ("" if x is None else (str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)))

def hook_sheet_get(who, sid, rng):
    _seed(); s = ST["sheets"].get(sid)
    if s is None: return None
    if s["owner"] != who: return _err(404, "Requested entity was not found.")
    try: return _values(s, rng)
    except ValueError as e: return _err(400, str(e))

def hook_sheet_put(who, sid, rng, values):
    _seed(); s = ST["sheets"].get(sid)
    if s is None: return None
    if s["owner"] != who: return _err(404, "Requested entity was not found.")
    try: t, r1, c1, _r2, _c2 = parse_range(rng, s)
    except ValueError as e: return _err(400, str(e))
    for i, row in enumerate(values or []):
        while len(t["rows"]) < r1 + i: t["rows"].append([])
        tr = t["rows"][r1 - 1 + i]
        while len(tr) < c1 - 1 + len(row): tr.append("")
        for j, x in enumerate(row): tr[c1 - 1 + j] = _cell(x)
    return {"updatedRange": f"{t['title']}!{_col(c1)}{r1}", "updatedRows": len(values or []), "updatedColumns": max((len(r) for r in values or []), default=0), "updatedCells": sum(len(r) for r in values or [])}

def _props(t, i): return {"sheetId": i, "title": t["title"], "index": i, "sheetType": "GRID", "gridProperties": {"rowCount": max(1000, len(t["rows"])), "columnCount": 26}}

def _meta(s): return {"spreadsheetId": s["id"], "properties": {"title": s["title"]}, "sheets": [{"properties": _props(t, i)} for i, t in enumerate(s["tabs"])], "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{s['id']}/edit"}

async def sheet_meta(req):
    who = _H["who"](req, "google", SH_R, SH_W); s, e = _sheet(req, who)
    return e or web.json_response(_meta(s))

async def sheet_create(req):
    who = _H["who"](req, "google", SH_W); _seed(); b = await req.json()
    tabs = [{"title": str(((x.get("properties") or {}).get("title")) or "Sheet1"), "rows": []} for x in (b.get("sheets") or [])] or [{"title": "Sheet1", "rows": []}]
    s = {"id": _nid("sh"), "owner": who, "title": str((b.get("properties") or {}).get("title") or "Untitled spreadsheet"), "tabs": tabs}
    ST["sheets"][s["id"]] = s
    return web.json_response(_meta(s))

async def sheet_batch(req):
    who = _H["who"](req, "google", SH_W); s, e = _sheet(req, who)
    if e: return e
    reqs = (await req.json()).get("requests") or []; ST["batches"].append({"who": who, "sheet": s["id"], "requests": reqs}); replies = []
    for r in reqs:
        if "addSheet" in r:
            title = str(((r["addSheet"] or {}).get("properties") or {}).get("title") or f"Sheet{len(s['tabs']) + 1}")
            if any(t["title"] == title for t in s["tabs"]): return _err(400, f'Invalid requests[0].addSheet: A sheet with the name "{title}" already exists. Please enter another name.')
            s["tabs"].append({"title": title, "rows": []}); replies.append({"addSheet": {"properties": _props(s["tabs"][-1], len(s["tabs"]) - 1)}})
        else: return _err(400, "Invalid requests: unsupported request " + ",".join(r))
    return web.json_response({"spreadsheetId": s["id"], "replies": replies})

async def sheet_batch_get(req):
    who = _H["who"](req, "google", SH_R, SH_W); s, e = _sheet(req, who)
    if e: return e
    out = []
    for rg in req.query.getall("ranges", []):
        try: out.append(_values(s, rg))
        except ValueError as ex: return _err(400, str(ex))
    return web.json_response({"spreadsheetId": s["id"], "valueRanges": out})

async def sheet_append(req):
    who = _H["who"](req, "google", SH_W); s, e = _sheet(req, who)
    if e: return e
    rng = req.match_info["range"]; b = await req.json(); vals = b.get("values") or []
    try: t, _r1, c1, _r2, _c2 = parse_range(rng, s)
    except ValueError as ex: return _err(400, str(ex))
    if not vals or not all(isinstance(r, list) for r in vals): return _err(400, "Invalid values: expected a list of rows")
    while t["rows"] and not any(str(x) != "" for x in t["rows"][-1]): t["rows"].pop()
    first = len(t["rows"]) + 1
    for row in vals:
        r = [""] * (c1 - 1) + [_cell(x) for x in row]; t["rows"].append(r)
    w = max(len(r) for r in vals); ST["appends"].append({"who": who, "sheet": s["id"], "values": vals})
    return web.json_response({"spreadsheetId": s["id"], "tableRange": f"{t['title']}!A1", "updates": {"spreadsheetId": s["id"], "updatedRange": f"{t['title']}!{_col(c1)}{first}:{_col(c1 + w - 1)}{first + len(vals) - 1}",
                                                                                         "updatedRows": len(vals), "updatedColumns": w, "updatedCells": sum(len(r) for r in vals)}})

def routes(app, S, helpers):
    _H.update(helpers)
    app.add_routes([web.get("/v1/documents/{id}", doc_get), web.post("/v1/documents", doc_create), web.post("/v1/documents/{id}:batchUpdate", doc_batch),
                    web.get("/v4/spreadsheets/{sid}", sheet_meta), web.post("/v4/spreadsheets", sheet_create), web.post("/v4/spreadsheets/{sid}:batchUpdate", sheet_batch),
                    web.get("/v4/spreadsheets/{sid}/values:batchGet", sheet_batch_get), web.post("/v4/spreadsheets/{sid}/values/{range}:append", sheet_append)])
