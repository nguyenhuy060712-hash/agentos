"""GĐ12.1 one notify layer: destinations (where), subscriptions (which events), signed delivery with health.
emit(event_type, data, workspace_id, ...) -> one outbox row per matching destination (the exact signed bytes are stored once and re-sent on retry) -> send now -> notify.retry_due()
calls retry_due() here for the pending ones (now, +1, +5, +15 min with jitter). Keys are never stored here: a destination points at `ws_secrets` rows (signing key
`aos_dest_<id>_sign` is created for every webhook destination and shown ONCE on create / roll_secret). Egress always goes through netguard (notify._post).
Event types register themselves (register_event_type). Personal ("mine") destinations: owner_user_id set, only the owner and workspace admins see them (in the SQL).
Demo accounts: writes 403 (admin.allowed + _write guard) and emit() is a no-op (notify._demo)."""
import asyncio, base64, datetime as dt, fnmatch, hashlib, hmac, json, logging, random, re, secrets, time, uuid
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

import notify, ws_secrets

log = logging.getLogger("destinations")
router = APIRouter()
KINDS = ("email", "telegram", "slack_webhook", "webhook")
FILTER_KEYS = ("source_id", "project_id", "agent_id", "min_severity", "environment")
SEVERITY = {"info": 0, "warn": 1, "critical": 2}
FAIL_AFTER = 3                      # consecutive failed attempts -> FAILING
FAILING_SOLO_AFTER = dt.timedelta(hours=24)   # FAILING longer than this: one try per event, no retries
PATTERN_RE = re.compile(r"^[a-z0-9_.*]{1,80}$")
SENSITIVE_HDR = re.compile(r"authori[sz]ation|token|key|secret|cookie|password", re.I)
PUB = ("id, workspace_id, owner_user_id, name, kind, config, secret_refs, headers, signing_secret_ref, sig_extra, health, fail_reason, consecutive_failures, "
       "first_failure_at, last_success_at, last_attempt_at, enabled, created_by, created_at")

async def _q(*a, **k):   # nạp lười để test đơn vị không phải nạp cả deepagent_api
    import deepagent_api as A
    return await A.q(*a, **k)

# ------------------------------------------------------------------ event types
EVENT_TYPES: dict = {}

def register_event_type(name: str, schema: dict, owner_task: str):
    """Other cards register their own event types (data fields: {name: description}); emit() refuses unknown types."""
    EVENT_TYPES[name] = {"schema": schema, "owner_task": owner_task}

_ALERT = {"rule": "rule name", "project_id": "project id", "severity": "info|warn|critical", "metric": "label, e.g. Total Cost", "value": "number", "threshold": "number", "op": "> >= < <=",
          "unit": "usd|%|ms|s|(none)", "agg": "avg|sum|p95 ...", "window_min": "minutes", "workspace": "workspace name (optional)", "link": "url of the alert (optional)"}
for _n in ("alert.fired", "alert.resolved", "alert.renotify", "alert.test"): register_event_type(_n, _ALERT, "12.1")
for _n in ("system.health.warn", "system.health.critical"): register_event_type(_n, {"kind": "disk|ram|postgres", "text": "message", "severity": "warn|critical"}, "12.1")
register_event_type("destination.disabled", {"destination_id": "id", "name": "name", "reason": "why"}, "12.1")
for _n in ("rule.batch", "rule.error"): register_event_type(_n, {"rule": "rule name", "project_id": "project id", "text": "message"}, "12.1")

# ------------------------------------------------------------------ envelope, signing, matching
def new_key() -> str: return secrets.token_hex(32)

def sign(key: str, raw: bytes) -> str:
    """LangSmith style: sha256=<hex HMAC-SHA256(key, raw body)>."""
    return "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()

def sign_standard(key: str, msg_id: str, ts: str, raw: bytes) -> str:
    """Standard Webhooks: v1,<base64 HMAC-SHA256(key, "<id>.<ts>.<body>")>."""
    return "v1," + base64.b64encode(hmac.new(key.encode(), f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()

def envelope(event_type, data, event_id=None, request_id=None) -> dict:
    return {"id": str(event_id or uuid.uuid4()), "type": event_type, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "request_id": str(request_id or uuid.uuid4()), "data": data}

def encode(env: dict) -> bytes: return json.dumps(env, ensure_ascii=False, separators=(",", ":"), default=str).encode()

def describe(env: dict):
    """(title, text, link) for the text channels (email / telegram / slack)."""
    d = env.get("data") if isinstance(env.get("data"), dict) else {}
    return (str(d.get("title") or d.get("rule") or env["type"]), str(d.get("text") or d.get("message") or json.dumps(d, ensure_ascii=False, default=str)[:600]), d.get("link"))

def filter_ok(flt, ctx: dict) -> bool:
    """Every filter key must hold; a key the event cannot answer (field missing) does not match. Unknown keys are ignored."""
    for k, want in (flt or {}).items():
        if k == "min_severity":
            if SEVERITY.get(str(ctx.get("severity")), -1) < SEVERITY.get(str(want), 0): return False
        elif k in FILTER_KEYS:
            have = ctx.get(k)
            if have is None or str(have) not in {str(x) for x in (want if isinstance(want, list) else [want])}: return False
    return True

def channel_of(dest) -> dict:
    """The old notify channel dict for a destination (text senders, legacy callers)."""
    cfg = dest["config"] or {}
    if dest["kind"] == "webhook": return {"kind": "webhook", "target": cfg.get("url"), "signing_secret": dest.get("signing_secret_ref")}
    return {**cfg, "kind": "slack" if dest["kind"] == "slack_webhook" else dest["kind"]}

# ------------------------------------------------------------------ keys
def _sign_name(did) -> str: return f"aos_dest_{did}_sign"

async def _put_secret(ws, name, value, host, by):
    """Create / replace a ws_secrets row; allowed_hosts = what it had + this destination's host."""
    old = await _q("SELECT allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s", (ws, name), one=True)
    hosts = sorted(set((old or {}).get("allowed_hosts") or []) | ({host.lower()} if host else set()))
    await _q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts,created_by) VALUES(%s,%s,%s,%s,%s) "
             "ON CONFLICT(workspace_id,name) DO UPDATE SET value_enc=EXCLUDED.value_enc, allowed_hosts=EXCLUDED.allowed_hosts, updated_at=now()",
             (ws, name, ws_secrets.seal_value(ws, name, value), hosts, by))

async def _signing_key(dest, url) -> str:
    ws, host, name = dest["workspace_id"], urlparse(url).hostname, dest.get("signing_secret_ref")
    if not name:   # e.g. a destination migrated from an alert channel that had no key: make one now (roll_secret shows it)
        name = _sign_name(dest["id"]); await _put_secret(ws, name, new_key(), host, "system")
        await _q("UPDATE notify_destinations SET signing_secret_ref=%s, secret_refs = secret_refs || %s WHERE id=%s", (name, Jsonb({"signing": name}), dest["id"]))
    try: return await ws_secrets.get_secret(name, ws, host)
    except (PermissionError, LookupError) as e: raise notify.Permanent(str(e))

# ------------------------------------------------------------------ delivery
def http_of(detail):
    m = re.match(r"HTTP (\d+)", detail or ""); return int(m.group(1)) if m else None

def reason_of(res) -> str:
    """Health reason of a failed attempt, from notify's result text (never contains a URL or key)."""
    d, code = res.get("detail") or "", http_of(res.get("detail"))
    if d.startswith("bị chặn"): return "BLOCKED_BY_POLICY"
    if code is not None:
        if code == 404: return "URL_NOT_FOUND"
        if code in (401, 403): return "AUTH_FAILED"
        if code == 429: return "RATE_LIMITED"
        return "SERVER_ERROR" if code >= 500 or code in (408, 425) else "CLIENT_ERROR"
    if "Timeout" in d: return "TIMEOUT"
    if "SSL" in d or "Certificate" in d: return "TLS"
    if d.startswith("SMTP 535"): return "AUTH_FAILED"
    return "CLIENT_ERROR" if res.get("permanent") else "CONNECTION"

async def _post_webhook(dest, row):
    cfg, ws = dest["config"] or {}, dest["workspace_id"]; url = cfg.get("url") or ""
    if not url.startswith(("http://", "https://")): raise notify.Permanent("URL webhook không hợp lệ")
    key = await _signing_key(dest, url); raw = bytes(row["raw_body"])
    hdr = {"Content-Type": "application/json", "X-LangSmith-Signature": sign(key, raw), "X-AOS-Event-Id": str(row["event_id"]),
           "X-AOS-Event-Type": row["event_type"], "X-AOS-Request-Id": str(row["request_id"])}
    if dest["sig_extra"] == "standard_webhooks":
        ts = str(int(time.time())); hdr.update({"webhook-id": str(row["event_id"]), "webhook-timestamp": ts, "webhook-signature": sign_standard(key, str(row["event_id"]), ts, raw)})
    try: custom = await ws_secrets.resolve_headers(dest["headers"] or {}, url, ws)
    except (PermissionError, LookupError) as e: raise notify.Permanent(str(e))
    return await notify._post(url, timeout=20, content=raw, headers={**(custom or {}), **hdr})

async def _health(dest, res, reason):
    """One attempt result -> the destination row after the update. 3 failed attempts in a row = FAILING, one success = HEALTHY."""
    if res["ok"]:
        return await _q("UPDATE notify_destinations SET health='HEALTHY', fail_reason=NULL, consecutive_failures=0, first_failure_at=NULL, last_success_at=now(), last_attempt_at=now() WHERE id=%s RETURNING " + PUB, (dest["id"],), one=True)
    return await _q("UPDATE notify_destinations SET consecutive_failures=consecutive_failures+1, first_failure_at=coalesce(first_failure_at, now()), last_attempt_at=now(), fail_reason=%s, "
                    "health=CASE WHEN consecutive_failures+1 >= %s THEN 'FAILING' ELSE health END WHERE id=%s RETURNING " + PUB, (reason, FAIL_AFTER, dest["id"]), one=True)

async def _attempt(row, once=False) -> dict:
    """One send of an outbox row. once=True: no retry afterwards (test button, FAILING > 24 h). Returns {ok, http_status, reason, latency_ms, health}."""
    dest = await _q("SELECT * FROM notify_destinations WHERE id=%s", (row["destination_id"],), one=True)
    t0 = time.perf_counter()
    if not dest: res = notify._res(False, "đích đã bị xoá", True)
    else:
        try:
            if dest["kind"] == "webhook": res = await _post_webhook(dest, row)
            else:
                ev = json.loads(bytes(row["raw_body"])); title, text, link = describe(ev)
                if dest["kind"] == "slack_webhook" and isinstance(ev.get("data"), dict) and not ev["data"].get("workspace"):   # the signed bytes stay as stored; only the Slack text gets the workspace name
                    w = await _q("SELECT name FROM workspaces WHERE id=%s", (dest["workspace_id"],), one=True)
                    if w: ev["data"] = {**ev["data"], "workspace": w["name"]}
                res = await notify.notify(dest["workspace_id"], channel_of(dest), title, text, link, ev)
        except notify.Permanent as e: res = notify._res(False, e, True)
        except Exception as e:  # noqa: BLE001
            res = notify._res(False, "lỗi: " + type(e).__name__)
    ms = int((time.perf_counter() - t0) * 1000); code = http_of(res["detail"]); n = row["attempts"] + 1
    reason = None if res["ok"] else reason_of(res); h = None
    if dest and not res.get("skipped"): h = await _health(dest, res, reason)
    st, wait = notify.transition(n, res)
    if st == "pending" and (once or (h and h["health"] == "FAILING" and h["first_failure_at"] and dt.datetime.now(dt.timezone.utc) - h["first_failure_at"] > FAILING_SOLO_AFTER)): st, wait = "failed", None
    if wait: wait = wait * random.uniform(0.8, 1.2)
    await _q("UPDATE outbox SET status=%s, attempts=%s, last_error=%s, detail=%s, http_status=%s, latency_ms=%s, "
             "next_at=CASE WHEN %s::float8 IS NULL THEN NULL ELSE now() + %s::float8 * interval '1 minute' END WHERE id=%s",
             (st, n, None if res["ok"] else res["detail"], res["detail"], code, ms, wait, wait, row["id"]))
    if code == 410 and dest and dest["enabled"]:   # the receiver says "gone": stop sending, tell the others
        await _q("UPDATE notify_destinations SET enabled=false WHERE id=%s", (dest["id"],))
        try: await emit("destination.disabled", {"destination_id": dest["id"], "name": dest["name"], "reason": "HTTP 410"}, dest["workspace_id"])
        except Exception: log.exception("destination.disabled emit failed")  # noqa: BLE001
    return {"ok": res["ok"], "http_status": code, "reason": reason, "latency_ms": ms, "health": h["health"] if h else None, "detail": res["detail"]}

async def _insert(dest, sub_id, env, raw, ws):
    title, text, _ = describe(env)
    return await _q("INSERT INTO outbox(kind,target,subject,body,status,detail,ws_id,attempts,next_at,destination_id,subscription_id,event_id,event_type,request_id,raw_body) "
                    "VALUES(%s,%s,%s,%s,'pending','đang gửi',%s,0,now(),%s,%s,%s,%s,%s,%s) RETURNING *",
                    (dest["kind"], dest["name"][:200], title, text, ws, dest["id"], sub_id, env["id"], env["type"], env["request_id"], raw), one=True)

async def emit(event_type, data, workspace_id, project_id=None, user_id=None, collect=None) -> int:
    """Deliver an event to every enabled destination of the workspace subscribed to it. Returns the number of destinations addressed. Demo: no-op.
    Personal destinations only get events with no user or their owner's. A filter key the event cannot answer does not match.
    collect (a list): gets one {destination_id, destination, ok, http_status, reason, latency_ms, health} per destination addressed (alerts "send test")."""
    if notify._demo(): return 0
    if event_type not in EVENT_TYPES: raise ValueError("unregistered event type: " + event_type)
    ws = int(workspace_id)
    ctx = {**(data if isinstance(data, dict) else {}), **({"project_id": project_id} if project_id is not None else {})}
    subs = await _q("SELECT s.id sub_id, s.event_type pat, s.filter, d.* FROM notify_subscriptions s JOIN notify_destinations d ON d.id=s.destination_id "
                    "WHERE s.workspace_id=%s AND d.workspace_id=%s AND d.enabled ORDER BY s.id", (ws, ws))
    chosen = {}
    for s in subs:
        if s["id"] in chosen or not fnmatch.fnmatchcase(event_type, s["pat"]) or not filter_ok(s["filter"], ctx): continue
        if s["owner_user_id"] is not None and user_id is not None and s["owner_user_id"] != user_id: continue
        chosen[s["id"]] = s
    if not chosen: return 0
    env = envelope(event_type, data); raw = encode(env)
    rows = [(await _insert(d, d["sub_id"], env, raw, ws)) for d in chosen.values()]
    for d, r in zip(chosen.values(), await asyncio.gather(*(_attempt(r) for r in rows), return_exceptions=True)):
        if isinstance(r, Exception): log.error("delivery failed: %s", type(r).__name__)
        if collect is not None:
            collect.append({"destination_id": d["id"], "destination": d["name"], **({"ok": False, "http_status": None, "reason": "ERROR", "latency_ms": None, "health": None} if isinstance(r, Exception)
                            else {k: r[k] for k in ("ok", "http_status", "reason", "latency_ms", "health")})})
    return len(rows)

async def send_test(dest) -> dict:
    """Real single send of an alert.test event (also when disabled); no retry; written to the outbox like any delivery."""
    env = envelope("alert.test", {"title": "Moza Agent OS - test", "text": "This is a test message / Đây là tin thử.", "rule": "test"})
    row = await _insert(dest, None, env, encode(env), dest["workspace_id"])
    return await _attempt(row, once=True)

async def retry_due():
    """Worker task (called from notify.retry_due): destination deliveries whose next try has come. A 2 minute lease stops a second process taking the same row."""
    for r in await _q("UPDATE outbox SET next_at = now() + interval '2 minutes' WHERE id IN (SELECT id FROM outbox WHERE status='pending' AND destination_id IS NOT NULL AND next_at <= now() "
                      "ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED) RETURNING *"):
        try: await _attempt(r)
        except Exception:  # noqa: BLE001
            log.exception("destination retry failed")
            await _q("UPDATE outbox SET attempts=attempts+1, status=CASE WHEN attempts+1 > %s THEN 'failed' ELSE status END WHERE id=%s", (len(notify.BACKOFF_MIN), r["id"]))

async def channels_for(ws, event_type) -> list:
    """Old-style channel dicts of the workspace-level, enabled destinations subscribed to this event type (insights weekly report uses it)."""
    out, seen = [], set()
    for s in await _q("SELECT s.event_type pat, d.* FROM notify_subscriptions s JOIN notify_destinations d ON d.id=s.destination_id "
                      "WHERE s.workspace_id=%s AND d.workspace_id=%s AND d.enabled AND d.owner_user_id IS NULL ORDER BY d.id, s.id", (int(ws), int(ws))):
        if s["id"] not in seen and fnmatch.fnmatchcase(event_type, s["pat"]): seen.add(s["id"]); out.append(channel_of(s))
    return out

# ------------------------------------------------------------------ API
def _user():
    import admin
    u = admin.cur()
    if not u: raise HTTPException(401, "Cần đăng nhập")
    return u

def _admin(u) -> bool: return u.get("role") == "admin" and not u.get("demo")

def _write(u):
    if u.get("demo"): raise HTTPException(403, "Tài khoản demo chỉ được xem")

def _refs(config, headers, signing=None) -> dict:
    names = set(ws_secrets.REF.findall(json.dumps([config, headers])))
    if isinstance(config.get("secret"), str): names.add(config["secret"])
    if signing: names.add(signing)
    return {"names": sorted(names)}

def _validate(kind, config, headers):
    config = dict(config or {})
    if len(json.dumps(config)) > 4000 or len(json.dumps(headers or {})) > 4000: raise HTTPException(422, "Cấu hình quá lớn")
    try:
        if kind == "webhook": notify.validate_channel("webhook", config.get("url"), {})
        else: notify.validate_channel("slack" if kind == "slack_webhook" else kind, config.get("target"), config)
    except ValueError as e: raise HTTPException(422, str(e))
    if config.get("password") is not None and not ws_secrets.REF.fullmatch(str(config["password"]).strip()): raise HTTPException(422, "password phải là tham chiếu {{secret:TÊN}}")
    for k, v in (headers or {}).items():
        if not re.match(r"^[A-Za-z0-9\-]{1,60}$", k) or "\n" in str(v) or "\r" in str(v): raise HTTPException(422, "Tên/giá trị header không hợp lệ")
        if SENSITIVE_HDR.search(k) and not ws_secrets.REF.fullmatch(str(v).strip()): raise HTTPException(422, f"Header {k} phải là tham chiếu {{{{secret:TÊN}}}}")
    return config

def _check_sub(event_type, flt):
    if not PATTERN_RE.match(event_type or ""): raise HTTPException(422, "event_type: chữ thường, số, _ . và * (vd alert.*)")
    bad = [k for k in (flt or {}) if k not in FILTER_KEYS]
    if bad: raise HTTPException(422, "Khoá lọc không hợp lệ: " + ", ".join(bad))

class DestIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["email", "telegram", "slack_webhook", "webhook"]
    config: dict = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    sig_extra: Literal["standard_webhooks"] | None = None
    enabled: bool = True
    events: list[str] = Field(default_factory=list, max_length=20)   # subscription patterns to create right away

class DestUpd(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    config: dict | None = None
    headers: dict[str, str] | None = None
    sig_extra: Literal["standard_webhooks"] | None = None
    clear_sig_extra: bool = False
    enabled: bool | None = None

class SubIn(BaseModel):
    destination_id: int
    event_type: str
    filter: dict = Field(default_factory=dict)

class SubUpd(BaseModel):
    event_type: str | None = None
    filter: dict | None = None

async def _get(did, u, need_owner=False):
    """Visible destination (workspace + owner-or-admin, in the query). need_owner: a "mine" destination of this user."""
    r = await _q(f"SELECT {PUB} FROM notify_destinations WHERE id=%s AND workspace_id=%s AND (owner_user_id=%s OR %s)", (did, u["ws"], u["id"], _admin(u)), one=True)
    if not r or (need_owner and r["owner_user_id"] != u["id"]): raise HTTPException(404, "Không có đích này")
    return r

async def _create(u, b: DestIn, owner):
    _write(u); config = _validate(b.kind, b.config, b.headers)
    for e in b.events: _check_sub(e, {})
    r = await _q("INSERT INTO notify_destinations(workspace_id,owner_user_id,name,kind,config,headers,sig_extra,enabled,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                 (u["ws"], owner, b.name, b.kind, Jsonb(config), Jsonb(b.headers), b.sig_extra, b.enabled, u["username"]), one=True)
    did, key, sref = r["id"], None, None
    if b.kind == "webhook":
        key, sref = new_key(), _sign_name(did)
        await _put_secret(u["ws"], sref, key, urlparse(config["url"]).hostname, u["username"])
    await _q("UPDATE notify_destinations SET signing_secret_ref=%s, secret_refs=%s WHERE id=%s", (sref, Jsonb(_refs(config, b.headers, sref)), did))
    for e in b.events: await _q("INSERT INTO notify_subscriptions(workspace_id,destination_id,event_type,created_by) VALUES(%s,%s,%s,%s)", (u["ws"], did, e, u["username"]))
    out = await _get(did, u)
    return {**out, "signing_key": key, "note": "Khoá ký chỉ hiện một lần / The signing key is shown once"} if key else out

async def _patch(did, b: DestUpd, u, need_owner=False):
    _write(u); d = await _get(did, u, need_owner)
    if d["owner_user_id"] is None and not _admin(u): raise HTTPException(403, "Chỉ admin sửa đích của workspace")
    config = _validate(d["kind"], b.config if b.config is not None else d["config"], b.headers if b.headers is not None else d["headers"])
    headers = b.headers if b.headers is not None else d["headers"]
    if d["kind"] == "webhook" and b.config is not None:   # new host: the signing key may be sent there
        if d["signing_secret_ref"]:
            await _q("UPDATE ws_secrets SET allowed_hosts = (SELECT array_agg(DISTINCT h) FROM unnest(allowed_hosts || %s::text) h), updated_at=now() WHERE workspace_id=%s AND name=%s",
                     (urlparse(config["url"]).hostname.lower(), u["ws"], d["signing_secret_ref"]))
    sig = None if b.clear_sig_extra else (b.sig_extra or d["sig_extra"])
    reset = b.enabled is True and not d["enabled"]   # switching a disabled destination back on starts the health count again
    await _q("UPDATE notify_destinations SET name=coalesce(%s,name), config=%s, headers=%s, sig_extra=%s, secret_refs=%s, enabled=coalesce(%s,enabled)"
             + (", health='HEALTHY', fail_reason=NULL, consecutive_failures=0, first_failure_at=NULL" if reset else "") + " WHERE id=%s",
             (b.name, Jsonb(config), Jsonb(headers), sig, Jsonb(_refs(config, headers, d["signing_secret_ref"])), b.enabled, did))
    return await _get(did, u)

async def _delete(did, u, need_owner=False):
    _write(u); d = await _get(did, u, need_owner)
    if d["owner_user_id"] is None and not _admin(u): raise HTTPException(403, "Chỉ admin xoá đích của workspace")
    await _q("DELETE FROM notify_destinations WHERE id=%s", (did,))
    await _q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (u["ws"], _sign_name(did)))   # only the key made for this destination
    return {"ok": True}

@router.get("/api/notify/event_types")
async def event_types():
    _user(); return [{"name": k, **v} for k, v in sorted(EVENT_TYPES.items())]

@router.get("/api/notify/destinations")
async def list_destinations():
    u = _user()
    return await _q(f"SELECT {PUB}, (SELECT coalesce(json_agg(json_build_object('id', s.id, 'event_type', s.event_type, 'filter', s.filter) ORDER BY s.id), '[]') FROM notify_subscriptions s WHERE s.destination_id=d.id) subscriptions "
                    "FROM notify_destinations d WHERE workspace_id=%s AND (owner_user_id=%s OR %s) ORDER BY id", (u["ws"], u["id"], _admin(u)))

@router.post("/api/notify/destinations")
async def create_destination(b: DestIn):
    u = _user()
    if not _admin(u): raise HTTPException(403, "Chỉ admin tạo đích của workspace (đích cá nhân: /api/me/destinations)")
    return await _create(u, b, None)

@router.get("/api/notify/destinations/{did}")
async def get_destination(did: int): return await _get(did, _user())

@router.patch("/api/notify/destinations/{did}")
async def patch_destination(did: int, b: DestUpd): return await _patch(did, b, _user())

@router.delete("/api/notify/destinations/{did}")
async def delete_destination(did: int): return await _delete(did, _user())

@router.post("/api/notify/destinations/{did}/test")
async def test_destination(did: int):
    u = _user(); _write(u); d = await _get(did, u)
    if d["owner_user_id"] is None and not _admin(u): raise HTTPException(403, "Chỉ admin thử đích của workspace")
    r = await send_test(d)
    return {k: r[k] for k in ("ok", "http_status", "reason", "latency_ms", "health")}

@router.post("/api/notify/destinations/{did}/roll_secret")
async def roll_secret(did: int):
    u = _user(); _write(u); d = await _get(did, u)
    if d["owner_user_id"] is None and not _admin(u): raise HTTPException(403, "Chỉ admin đổi khoá")
    if d["kind"] != "webhook": raise HTTPException(422, "Chỉ webhook có khoá ký")
    key, name = new_key(), d["signing_secret_ref"] or _sign_name(did)
    await _put_secret(u["ws"], name, key, urlparse(d["config"]["url"]).hostname, u["username"])   # the old key stops verifying at once: every later delivery is signed with the new one
    await _q("UPDATE notify_destinations SET signing_secret_ref=%s, secret_refs=%s WHERE id=%s", (name, Jsonb(_refs(d["config"], d["headers"], name)), did))
    return {"signing_key": key, "note": "Khoá mới chỉ hiện một lần; khoá cũ hết hiệu lực ngay / The new key is shown once; the old key stops working now"}

@router.get("/api/notify/destinations/{did}/deliveries")
async def deliveries(did: int):
    u = _user(); await _get(did, u)
    return await _q("SELECT id, event_id, event_type, request_id, status, attempts, http_status, latency_ms, detail, last_error, created, next_at FROM outbox "
                    "WHERE destination_id=%s AND ws_id=%s ORDER BY id DESC LIMIT 100", (did, u["ws"]))

@router.get("/api/notify/subscriptions")
async def list_subscriptions(destination_id: int | None = None):
    u = _user()
    return await _q("SELECT s.id, s.destination_id, s.event_type, s.filter, s.created_at FROM notify_subscriptions s JOIN notify_destinations d ON d.id=s.destination_id "
                    "WHERE s.workspace_id=%s AND (d.owner_user_id=%s OR %s) AND (%s::int IS NULL OR s.destination_id=%s) ORDER BY s.id", (u["ws"], u["id"], _admin(u), destination_id, destination_id))

@router.post("/api/notify/subscriptions")
async def create_subscription(b: SubIn):
    u = _user(); _write(u); await _get(b.destination_id, u); _check_sub(b.event_type, b.filter)
    return await _q("INSERT INTO notify_subscriptions(workspace_id,destination_id,event_type,filter,created_by) VALUES(%s,%s,%s,%s,%s) RETURNING id, destination_id, event_type, filter, created_at",
                    (u["ws"], b.destination_id, b.event_type, Jsonb(b.filter), u["username"]), one=True)

async def _sub(sid, u):
    r = await _q("SELECT s.* FROM notify_subscriptions s JOIN notify_destinations d ON d.id=s.destination_id WHERE s.id=%s AND s.workspace_id=%s AND (d.owner_user_id=%s OR %s)", (sid, u["ws"], u["id"], _admin(u)), one=True)
    if not r: raise HTTPException(404, "Không có đăng ký này")
    return r

@router.patch("/api/notify/subscriptions/{sid}")
async def patch_subscription(sid: int, b: SubUpd):
    u = _user(); _write(u); s = await _sub(sid, u)
    et, flt = b.event_type or s["event_type"], b.filter if b.filter is not None else s["filter"]; _check_sub(et, flt)
    return await _q("UPDATE notify_subscriptions SET event_type=%s, filter=%s WHERE id=%s RETURNING id, destination_id, event_type, filter, created_at", (et, Jsonb(flt), sid), one=True)

@router.delete("/api/notify/subscriptions/{sid}")
async def delete_subscription(sid: int):
    u = _user(); _write(u); await _sub(sid, u)
    await _q("DELETE FROM notify_subscriptions WHERE id=%s", (sid,)); return {"ok": True}

# ---- "mine": personal destinations (owner + admin see them; the list below shows only the caller's own)
@router.get("/api/me/destinations")
async def my_destinations():
    u = _user()
    return await _q(f"SELECT {PUB} FROM notify_destinations WHERE workspace_id=%s AND owner_user_id=%s ORDER BY id", (u["ws"], u["id"]))

@router.post("/api/me/destinations")
async def create_my_destination(b: DestIn):
    u = _user(); return await _create(u, b, u["id"])

@router.patch("/api/me/destinations/{did}")
async def patch_my_destination(did: int, b: DestUpd): return await _patch(did, b, _user(), need_owner=True)

@router.delete("/api/me/destinations/{did}")
async def delete_my_destination(did: int): return await _delete(did, _user(), need_owner=True)
