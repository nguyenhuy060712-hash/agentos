"""GĐ12.17 full-text search in traces (tr_runs.search_tsv), reusable by datasets later.
Words: lower-case, Vietnamese diacritics removed (python twin of the SQL function aos_fold), split on anything that is not a letter or digit.
Index (built by obs.upsert_run, filled for old rows by `backfill`): inputs weight A, outputs weight B, error / tags / metadata weight C.
Encrypted workspace (GĐ9.7 trace encryption): every word is replaced by a keyed hash (crypto.mac, the workspace 'mac' key) so the table holds no readable word and the
server can still find a WHOLE word; part of a word, prefixes and phrases are not available there (`info()` says so, the UI shows it)."""
import json, re, unicodedata
import crypto

FIELD_MAX = 32 * 1024     # first 32 KB of text per field
WORD_MAX = 64             # longer "words" (base64 blobs, hashes) are not indexed - same rule on the query side
DISTINCT_MAX = 4000       # distinct words kept per field
QUERY_MAX = 12            # words of one search
SCOPES = ("all", "input", "output")
_LABEL = {"all": "", "input": ":A", "output": ":B"}
_WORD = re.compile(r"[^\W_]+")
_STOP = """a an and are as at be but by for from has have i if in is it its my of on or so that the this to was were will with you your
và là của có không cho các một những được trong với này đó khi để cũng như thì mà đã sẽ đang ra vào lên về tôi bạn anh chị em ở"""

def fold(s) -> str:
    """lower-case + strip diacritics (đ -> d). Same result as the SQL function aos_fold on Vietnamese text."""
    out = []
    for ch in str(s or "").lower():
        if ch == "đ": out.append("d"); continue
        out.append("".join(c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c)) or ch)
    return "".join(out)

STOP = frozenset(fold(w) for w in _STOP.split())   # small vi + en list, folded like the query words

def tokens(text, limit=None) -> list:
    """Distinct folded words of `text`, in order of appearance (words over WORD_MAX characters are dropped)."""
    seen, out = set(), []
    for w in _WORD.findall(fold(text)):
        if len(w) > WORD_MAX or w in seen: continue
        seen.add(w); out.append(w)
        if limit and len(out) >= limit: break
    return out

def _leaves(v, acc, size=0):
    """String / number leaves of a JSON value (keys are noise: 'messages', 'content'), up to FIELD_MAX characters."""
    stack = [v]
    while stack and size < FIELD_MAX:
        x = stack.pop()
        if isinstance(x, str): acc.append(x); size += len(x)
        elif isinstance(x, (int, float)) and not isinstance(x, bool): acc.append(str(x)); size += 8
        elif isinstance(x, dict): stack.extend(reversed(list(x.values())))
        elif isinstance(x, (list, tuple)): stack.extend(reversed(list(x)))
    return acc

def _text(v) -> str:
    if v is None: return ""
    if isinstance(v, str): return v[:FIELD_MAX]
    return " ".join(_leaves(v, []))[:FIELD_MAX]

def blind(word, ws) -> str:
    """Lexeme of a word in an encrypted workspace: 'h' + hex HMAC (always starts with a letter so the text parser keeps it as ONE word)."""
    return "h" + crypto.mac(word, int(ws))[3:]

def tsv_text(inputs=None, outputs=None, error=None, tags=None, metadata=None, ws=None) -> tuple:
    """(A, B, C) strings for `setweight(to_tsvector('simple', x), 'A'|'B'|'C')`: space-joined folded words (readable) or keyed hashes when `ws` is given."""
    c = _text(error) + " " + " ".join(str(t) for t in (tags or [])) + " " + _text(metadata)
    res = [tokens(_text(inputs), DISTINCT_MAX), tokens(_text(outputs), DISTINCT_MAX), tokens(c[:FIELD_MAX], DISTINCT_MAX)]
    if ws is not None: res = [[blind(w, ws) for w in ws_] for ws_ in res]
    return tuple(" ".join(r) for r in res)

TSV_SQL = "setweight(to_tsvector('simple', %s), 'A') || setweight(to_tsvector('simple', %s), 'B') || setweight(to_tsvector('simple', %s), 'C')"

def query(text, scope="all", ws=None):
    """tsquery text for `to_tsquery('simple', ...)`: AND of the folded words (stop words dropped unless nothing else is left), restricted to inputs ('input') or
    outputs ('output'). ws given (encrypted workspace) -> the readable query OR the keyed-hash query (rows written before encryption was switched on stay findable).
    None when the text holds no word (the caller falls back to a plain substring match)."""
    if scope not in SCOPES: raise ValueError("scope")
    ws_ = tokens(text, QUERY_MAX); keep = [w for w in ws_ if w not in STOP] or ws_
    if not keep: return None
    lab = _LABEL[scope]
    plain = " & ".join(f"'{w}'{lab}" for w in keep)
    if ws is None: return plain
    return f"({plain}) | (" + " & ".join(f"'{blind(w, ws)}'{lab}" for w in keep) + ")"

def encrypted_ws(ws):
    """The workspace id when its traces are stored encrypted (search is then by whole word only), else None."""
    import aos_crypto
    return int(ws) if ws is not None and aos_crypto.on("traces", ws) else None

def info(ws):
    """What the UI tells the user about the search of this workspace."""
    if encrypted_ws(ws) is None: return {"encrypted": False, "mode": "full", "message": ""}
    return {"encrypted": True, "mode": "whole_word",
            "message": "Trace được mã hoá: chỉ tìm được nguyên từ (không tìm theo một phần của từ), và chỉ với trace đã được lập chỉ mục. / "
                       "Traces are encrypted: search finds whole words only (no part of a word), and only traces that are already indexed."}

async def backfill(batch=2000):
    """Index rows whose search_tsv is NULL, newest first (system context: sees every workspace). Plaintext rows get readable lexemes; rows with an encrypted envelope
    (or in a workspace with trace encryption) are decrypted here with the server key and stored as blind lexemes. A row that cannot be read gets an empty vector
    so it is never picked again. Returns the number of rows indexed."""
    import aos_crypto, deepagent_api as A
    rows = await A.q("""SELECT t.id, t.start_time, t.inputs, t.outputs, t.error, t.tags, t.metadata, p.workspace_id ws FROM tr_runs t JOIN projects p ON p.id=t.project_id
                        WHERE t.search_tsv IS NULL ORDER BY t.start_time DESC LIMIT %s""", (batch,))
    for r in rows:
        ins, outs, ws = r["inputs"], r["outputs"], r["ws"]; enc = aos_crypto.is_env(ins) or aos_crypto.is_env(outs) or aos_crypto.on("traces", ws)
        try:
            if aos_crypto.is_env(ins): ins = aos_crypto.unwrap_json(ins, key="inputs", row_id=r["id"])
            if aos_crypto.is_env(outs): outs = aos_crypto.unwrap_json(outs, key="outputs", row_id=r["id"])
            parts = tsv_text(ins, outs, r["error"], r["tags"], r["metadata"], ws if enc else None)
        except Exception:  # noqa: BLE001  (key missing / wrong envelope: leave it unsearchable, never retry forever)
            parts = ("", "", "")
        await A.q("UPDATE tr_runs SET search_tsv = " + TSV_SQL + " WHERE id=%s AND start_time=%s AND search_tsv IS NULL", (*parts, r["id"], r["start_time"]))
    return len(rows)
