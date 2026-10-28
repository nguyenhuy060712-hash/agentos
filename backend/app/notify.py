"""GĐ6.4 gửi cảnh báo thật: Telegram / Email (SMTP) / Slack / webhook (ký HMAC). Dùng chung cho Insights/Engine GĐ7.
Bí mật chỉ tham chiếu theo tên ({{secret:NAME}}), chỉ được thay khi máy đích nằm trong allowed_hosts (ws_secrets.py).
Outbox ghi mọi lần gửi: pending|sent|failed, attempts, last_error, next_at; thử lại 3 lần cách 1, 5, 15 phút (retry_due, chạy trong worker)."""
import asyncio, hashlib, hmac, html, json, logging, os, re, smtplib, ssl, sys, time
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb

import netguard, ws_secrets

BACKOFF_MIN = (1, 5, 15)
KINDS = ("telegram", "email", "slack", "webhook")
TG_BASE = "https://api.telegram.org"
SLACK_HOST = "hooks.slack.com"

async def _q(*a, **k):   # nạp lười để test đơn vị không phải nạp cả deepagent_api
    import deepagent_api as A
    return await A.q(*a, **k)

class Permanent(Exception):
    """Lỗi không thể khắc phục bằng gửi lại (sai cấu hình, máy đích không được phép, 4xx)."""

def _demo():   # admin đã nạp trong server; chưa nạp (test đơn vị) thì không có ngữ cảnh người dùng => không phải demo
    m = sys.modules.get("admin"); return bool(m and m.is_demo())

def _res(ok, detail, permanent=False): return {"ok": ok, "detail": str(detail)[:300], "permanent": permanent}

def validate_channel(kind, target, cfg):
    cfg = cfg or {}
    if kind == "telegram" and not (target and cfg.get("secret")): raise ValueError("Telegram cần chat_id (ô đích) và tên bí mật chứa token bot")
    if kind == "slack" and not cfg.get("secret"): raise ValueError("Slack cần tên bí mật chứa URL incoming webhook")
    if kind == "email" and not (target and (cfg.get("host") or os.environ.get("SMTP_HOST"))): raise ValueError("Email cần địa chỉ nhận và máy chủ SMTP (host hoặc env SMTP_HOST)")
    if kind == "webhook" and not (target or "").startswith(("http://", "https://")): raise ValueError("Webhook cần URL http(s)")
    for k in ("secret", "signing_secret"):
        if cfg.get(k) is not None and not re.match(ws_secrets.NAME_RE, str(cfg[k])): raise ValueError(f"{k}: tên bí mật không hợp lệ")

def channel_of(kind, target, cfg):
    """Gộp cột channel/target/channel_cfg của alert_rules thành dict channel cho notify()."""
    return {**(cfg or {}), "kind": kind, "target": target}

def _ref(name): return "{{secret:%s}}" % name

async def _secret(name, dest, ws):
    try: return await ws_secrets.resolve_secrets(_ref(name), dest, ws)
    except (PermissionError, LookupError) as e: raise Permanent(str(e))

def _classify(code):
    if 200 <= code < 300: return _res(True, f"HTTP {code}")
    return _res(False, f"HTTP {code}", permanent=400 <= code < 500 and code not in (408, 425, 429))

def _neterr(e):
    c = e
    for _ in range(6):   # a TLS failure is wrapped by httpx: look down the cause chain (type names only, never the message: it can hold the URL)
        if c is None: break
        if isinstance(c, ssl.SSLError): return "SSLError"
        c = c.__cause__ or c.__context__
    return type(e).__name__

async def _post(url, timeout=10, **kw):
    try:
        async with netguard.client("webhook", timeout=timeout) as h:   # GĐ10.2: một chính sách mạng chung (chặn SSRF, nối thẳng tới IP đã kiểm)
            r = await h.post(url, **kw)
        return _classify(r.status_code)
    except netguard.NetBlocked as e:   # reason không chứa URL/token; địa chỉ bị chặn thì gửi lại cũng không khác
        return _res(False, "bị chặn: " + e.reason, permanent=not e.reason.startswith("host does not resolve"))
    except httpx.HTTPError as e:   # không đưa str(e) vào log: có thể chứa URL kèm token
        return _res(False, "lỗi mạng: " + _neterr(e))

async def _telegram(ws, ch, title, text, link, event=None):
    base = (os.environ.get("TELEGRAM_API_BASE") or TG_BASE).rstrip("/")
    token = await _secret(ch["secret"], base + "/", ws)
    msg = f"<b>{html.escape(title)}</b>\n{html.escape(text)}"
    if link: msg += f'\n<a href="{html.escape(link, quote=True)}">Mở</a>'
    return await _post(f"{base}/bot{token}/sendMessage", json={"chat_id": ch["target"], "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})

_OPS = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "="}

def _num(v, unit):
    try: x = float(v)
    except (TypeError, ValueError): return str(v)
    return {"usd": f"${x:,.2f}", "%": f"{x:g}%", "ms": f"{x:g} ms", "s": f"{x:g} s"}.get(unit or "", f"{x:g}")

def _mrk(x): return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")   # Slack mrkdwn escapes only these three

def render_slack(event) -> dict:
    """GĐ12.2 Slack message of an event (envelope {type, data}). alert.*: Block Kit (header, value-threshold line, buttons 'View alert' / 'View runs' from AOS_PUBLIC_URL;
    no buttons when it is unset); a resolved alert has a green 'Resolved' header. Other events: title + text. Always has the plain `text` fallback."""
    t, d = str(event.get("type") or ""), event.get("data") if isinstance(event.get("data"), dict) else {}
    base = os.environ.get("AOS_PUBLIC_URL", "").rstrip("/")
    if not t.startswith("alert."):
        title = str(d.get("title") or d.get("rule") or t); text = str(d.get("text") or d.get("message") or "")
        body = f"*{_mrk(title)}*\n{_mrk(text)}" + (f"\n<{d['link']}|Mở>" if d.get("link") else "")
        return {"text": f"{title}: {text}"[:300], "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}}]}
    name, ws = str(d.get("rule") or t), d.get("workspace")
    head = ("✅ Resolved: " if t == "alert.resolved" else "⚠ ") + name + (f" – {ws}" if ws else "")
    unit = d.get("unit")
    line = f"{d.get('metric') or 'value'}: {_num(d.get('value'), unit)}"
    if d.get("threshold") is not None: line += f" {_OPS.get(str(d.get('op')), str(d.get('op') or ''))} {_num(d.get('threshold'), unit)}"
    if d.get("agg"): line += f" · {d['agg']}"
    if d.get("window_min"): line += f" · {d['window_min']} min"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": head[:150]}}, {"type": "section", "text": {"type": "mrkdwn", "text": _mrk(line)[:2900]}}]
    if base:
        pid = d.get("project_id")
        btn = lambda label, url: {"type": "button", "text": {"type": "plain_text", "text": label}, "url": url}
        blocks.append({"type": "actions", "elements": [btn("View alert", d.get("link") or f"{base}/rules"), btn("View runs", f"{base}/projects" + (f"?p={pid}" if pid else ""))]})
    return {"text": f"{head}: {line}"[:300], "blocks": blocks}

async def _slack(ws, ch, title, text, link, event=None):
    host = os.environ.get("SLACK_HOOK_HOST") or SLACK_HOST
    url = await _secret(ch["secret"], f"https://{host}/", ws)
    if urlparse(url).hostname != host.split(":")[0]: raise Permanent("URL Slack phải thuộc máy " + host)
    if event: return await _post(url, json=render_slack(event))
    body = f"*{title}*\n{text}" + (f"\n<{link}|Mở>" if link else "")
    return await _post(url, json={"text": body})

async def _webhook(ws, ch, title, text, link, event=None):
    url = ch.get("target") or ch.get("url")
    sig = ch.get("signing_secret")
    if not sig and not await asyncio.to_thread(netguard.is_internal, url): raise Permanent("Webhook ra ngoài cần signing_secret (bí mật có allowed_hosts chứa máy đích)")
    raw = json.dumps({"subject": title, "body": text, "title": title, "text": text, "link": link}, ensure_ascii=False).encode()
    hdr = {"Content-Type": "application/json"}
    if sig:
        key = await _secret(sig, url, ws); ts = str(int(time.time()))
        hdr["X-AOS-Timestamp"] = ts
        hdr["X-AOS-Signature"] = "sha256=" + hmac.new(key.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return await _post(url, content=raw, headers=hdr)

def email_html(title, text, link, event=None) -> str:
    """GĐ12.2 HTML part: a small table. EVERY value goes through html.escape (alert names / texts come from users and from the traced app); only http(s) links become <a>."""
    e = lambda v: html.escape(str(v))
    d = (event or {}).get("data") if isinstance((event or {}).get("data"), dict) else {}
    rows = [("Message", text)]
    for k, v in d.items():
        if k in ("title", "text", "message", "link"): continue
        rows.append((k, v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(v, ensure_ascii=False, default=str)[:500]))
    if event: rows.append(("Event", event.get("type")))
    tr = "".join(f'<tr><th align="left" style="padding:4px 10px;border:1px solid #ddd">{e(k)}</th><td style="padding:4px 10px;border:1px solid #ddd">{e(v)}</td></tr>' for k, v in rows)
    a = f'<p><a href="{html.escape(link, quote=True)}">{e(link)}</a></p>' if link and str(link).startswith(("http://", "https://")) else ""
    return f'<html><body><h3>{e(title)}</h3><table style="border-collapse:collapse">{tr}</table>{a}</body></html>'

def _smtp_send(host, port, user, pw, starttls, msg):
    ctx = ssl.create_default_context()
    s = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(host, port, timeout=15)
    with s:
        s.ehlo()
        if starttls and port != 465: s.starttls(context=ctx); s.ehlo()
        if user: s.login(user, pw or "")
        s.send_message(msg)

async def _email(ws, ch, title, text, link, event=None):
    host = ch.get("host") or os.environ.get("SMTP_HOST"); port = int(ch.get("port") or os.environ.get("SMTP_PORT") or 587)
    user = ch.get("user") or os.environ.get("SMTP_USER"); frm = ch.get("from") or os.environ.get("SMTP_FROM") or user or "agentos@localhost"
    sts = ch.get("starttls"); sts = (os.environ.get("SMTP_STARTTLS", "1") not in ("0", "false", "")) if sts is None else bool(sts)
    if not host: raise Permanent("Thiếu máy chủ SMTP")
    pw = ch.get("password")
    if isinstance(pw, str) and "{{" in pw:
        try: pw = await ws_secrets.resolve_secrets(pw, f"smtp://{host}:{port}", ws)
        except (PermissionError, LookupError) as e: raise Permanent(str(e))
    else: pw = pw or os.environ.get("SMTP_PASSWORD")
    to = ch.get("target") or ch.get("to"); to = [x.strip() for x in to.split(",")] if isinstance(to, str) else list(to or [])
    if not to: raise Permanent("Thiếu địa chỉ nhận")
    msg = EmailMessage(); msg["Subject"] = re.sub(r"[\r\n]+", " ", title); msg["From"] = frm; msg["To"] = ", ".join(re.sub(r"[\r\n]+", "", x) for x in to)
    msg.set_content(text + (f"\n\n{link}" if link else ""))
    msg.add_alternative(email_html(title, text, link, event), subtype="html")   # GĐ12.2: plain + simple HTML table
    try:
        await asyncio.to_thread(_smtp_send, host, port, user, pw, sts, msg)
    except smtplib.SMTPResponseException as e:
        return _res(False, f"SMTP {e.smtp_code}", permanent=500 <= e.smtp_code < 600)
    except (smtplib.SMTPException, OSError) as e:
        return _res(False, "lỗi SMTP/mạng: " + type(e).__name__)
    return _res(True, "SMTP đã nhận")

_SENDERS = {"telegram": _telegram, "slack": _slack, "webhook": _webhook, "email": _email}

async def notify(ws_id, channel: dict, title, text, link=None, event=None) -> dict:
    """Gửi MỘT lần, không ghi outbox. Trả {"ok","detail","permanent"} (permanent=True: đừng thử lại). Tài khoản demo: không gửi gì."""
    if _demo(): return {"ok": False, "detail": "demo: không gửi", "permanent": True, "skipped": True}
    f = _SENDERS.get((channel or {}).get("kind"))
    if not f: return _res(False, "kênh không hỗ trợ", True)
    try:
        return await f(int(ws_id or 1), channel, str(title), str(text), link, event)
    except Permanent as e: return _res(False, e, True)
    except (KeyError, ValueError) as e: return _res(False, "cấu hình kênh sai: " + type(e).__name__, True)
    except Exception as e:  # noqa: BLE001
        return _res(False, "lỗi: " + type(e).__name__)

def transition(attempts, res):
    """attempts = số lần đã thử (kể cả lần vừa xong). -> (status, phút chờ lần sau | None)."""
    if res["ok"]: return "sent", None
    if res.get("permanent") or attempts > len(BACKOFF_MIN): return "failed", None
    return "pending", BACKOFF_MIN[attempts - 1]

def _target(ch): return str(ch.get("target") or ch.get("to") or ch.get("url") or ch.get("secret") or "")[:200]

async def _attempt(row):
    res = await notify(row["ws_id"], row["channel"], row["subject"], row["body"], row["link"])
    n = row["attempts"] + 1
    st, wait = transition(n, res)
    await _q("UPDATE outbox SET status=%s, attempts=%s, last_error=%s, detail=%s, next_at=CASE WHEN %s::int IS NULL THEN NULL ELSE now() + %s::int * interval '1 minute' END WHERE id=%s",
             (st, n, None if res["ok"] else res["detail"], res["detail"], wait, wait, row["id"]))
    return st

async def send(ws_id, channel: dict, title, text, link=None) -> str:
    """Ghi outbox (pending) rồi gửi lần đầu; lỗi tạm thời sẽ được retry_due() thử lại. Trả chuỗi trạng thái. Demo: không ghi, không gửi."""
    if _demo(): return "demo: không gửi"
    r = await _q("INSERT INTO outbox(kind,target,subject,body,status,detail,ws_id,channel,link,attempts,next_at) VALUES(%s,%s,%s,%s,'pending','đang gửi',%s,%s,%s,0,now()) RETURNING *",
                 (channel["kind"], _target(channel), title, text, int(ws_id or 1), Jsonb(channel), link), one=True)
    st = await _attempt(r)
    return f"{st} ({channel['kind']})"

async def retry_due():
    for r in await _q("SELECT * FROM outbox WHERE status='pending' AND channel IS NOT NULL AND next_at <= now() ORDER BY id LIMIT 50"):
        try: await _attempt(r)
        except Exception:  # noqa: BLE001
            await _q("UPDATE outbox SET attempts=attempts+1, status=CASE WHEN attempts+1 > %s THEN 'failed' ELSE status END, next_at=now() + interval '5 minute' WHERE id=%s", (len(BACKOFF_MIN), r["id"]))
    try:   # GĐ12.1: deliveries to destinations (signed, with health) retry in the same worker tick; a failure here must not hide the legacy retries above
        import destinations; await destinations.retry_due()
    except Exception: logging.getLogger("notify").exception("destinations retry failed")  # noqa: BLE001
