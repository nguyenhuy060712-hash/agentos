// GĐ8.3b Zalo PERSONAL-account sidecar (23/10/2026 design, 24/10 build). UNOFFICIAL: uses zca-js 2.2.0 (reverse-engineered Zalo Web).
// Zalo may lock the number. Started by the backend (app/channel_zalo_personal.py) ONLY when AOS_ZALO_PERSONAL=1; never in the customer build.
//
// Security model
//  - listens on 127.0.0.1 ONLY; every request needs header x-zp-token == env ZP_TOKEN (random per backend process, constant-time compare)
//  - one zca-js session per binding id, kept IN MEMORY only. The login blob (cookie/imei/userAgent) is POSTed to the backend, which stores it
//    encrypted in ws secrets; the backend hands it back via /session/start after a restart. Nothing is written to disk, nothing logged.
//  - no friend-request / bulk / broadcast API exists here: only /send to ONE thread. Rate limits live in the backend (2 s spacing, daily cap).
//  - ZALO_PERSONAL_MOCK=1 -> fake zca client (mock_zca.mjs), zca-js is not even loaded: no Zalo network at all.
// Endpoints: POST /login/qr {binding_id} | POST /session/start {binding_id, session} | POST /session/stop {binding_id}
//            GET /status[?binding_id=] | POST /send {binding_id, thread_id, text, type: "user"|"group"}
//            mock only: POST /mock/scan {binding_id} | POST /mock/incoming {binding_id, ...} | GET /mock/sent?binding_id=
// Incoming messages -> POST {ZP_BACKEND}/internal/zalo-personal/{binding_id}/incoming (same token header).
import http from "node:http";
import { createHash, timingSafeEqual } from "node:crypto";
import { MockZalo, zcaShaped } from "./mock_zca.mjs";

const PORT = Number(process.env.ZP_PORT || 8790);
const TOKEN = process.env.ZP_TOKEN || "";
const BACKEND = (process.env.ZP_BACKEND || "http://127.0.0.1:8020").replace(/\/$/, "");
const MOCK = process.env.ZALO_PERSONAL_MOCK === "1";
const MAX_BODY = 1 << 20;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
if (!TOKEN || TOKEN.length < 16) { console.error("zalo-personal sidecar: ZP_TOKEN (>= 16 chars) is required"); process.exit(2); }
const log = (...a) => console.log("zalo-personal-sidecar:", ...a);

let zalo = null;                       // zca-js Zalo instance (real) or MockZalo
async function client() {
  if (zalo) return zalo;
  if (MOCK) zalo = new MockZalo();
  else { const { Zalo } = await import("zca-js"); zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false }); }
  return zalo;
}

const S = new Map();                   // binding_id -> {status, qr, name, error, api, ownId, retries}
const sess = (bid) => { let s = S.get(bid); if (!s) S.set(bid, s = { status: "none", qr: null, name: "", error: "", api: null, ownId: "", retries: 0 }); return s; };
const view = (bid) => { const s = S.get(bid); return s ? { status: s.status, qr: s.qr, name: s.name, error: s.error } : { status: "none", qr: null, name: "", error: "" }; };
const short = (e) => String(e?.message || e).replace(/\s+/g, " ").slice(0, 160);

async function toBackend(bid, path, payload) {
  const r = await fetch(`${BACKEND}/internal/zalo-personal/${bid}/${path}`, { method: "POST", headers: { "content-type": "application/json", "x-zp-token": TOKEN },
                                                                        body: JSON.stringify(payload), signal: AbortSignal.timeout(15000) });
  if (!r.ok) throw new Error(`backend ${path} -> HTTP ${r.status}`);
}

// ---- zca message -> plain incoming event for the backend
function fromZca(msg, ownId) {
  if (!msg || msg.isSelf) return null;
  const d = msg.data || {}, group = msg.type === 1, ct = d.content, mt = String(d.msgType || "");
  let text = "", files = [];
  if (typeof ct === "string") text = ct;
  else if (ct && typeof ct === "object" && ct.href && /photo|image/i.test(mt)) {
    files.push({ kind: "image", url: String(ct.href), name: String(ct.title || "image.jpg"), size: null }); text = String(ct.description || "");
  } else if (ct && typeof ct === "object" && ct.href && /file/i.test(mt)) {
    let size = null; try { size = Number(JSON.parse(ct.params || "{}").fileSize) || null; } catch { /* no size */ }
    files.push({ kind: "file", url: String(ct.href), name: String(ct.title || "file"), size }); text = String(ct.description || "");
  } else return null;                  // stickers, voice, links ... : not for the agent
  const mine = group ? (d.mentions || []).filter((m) => String(m.uid) === String(ownId)) : [];
  if (mine.length && typeof ct === "string") {   // cut our own @mention out of the text (positions refer to the original string)
    for (const m of [...mine].sort((a, b) => b.pos - a.pos)) text = text.slice(0, m.pos) + text.slice(m.pos + m.len);
    text = text.replace(/[ \t]{2,}/g, " ").trim();
  }
  if (!text.trim() && !files.length) return null;
  return { msg_id: String(d.msgId || ""), thread_id: String(msg.threadId), sender_id: String(d.uidFrom), sender_name: String(d.dName || ""),
           text, files, is_group: group, mentioned: mine.length > 0, ts: Number(d.ts) || Date.now() };
}

function attach(bid, api) {
  const s = sess(bid);
  s.api = api; s.status = "logged_in"; s.error = ""; s.qr = null;
  s.ownId = String(api.getOwnId());
  api.listener.on("message", (m) => {
    const ev = fromZca(m, s.ownId);
    if (!ev) return;
    toBackend(bid, "incoming", ev).catch((e) => log(`binding ${bid}: incoming not delivered: ${short(e)}`));
  });
  api.listener.on("closed", (code) => { if (S.get(bid) === s && s.api === api) { s.status = "disconnected"; s.error = `closed ${code}`; log(`binding ${bid}: connection closed (${code})`); } });
  api.listener.on("error", (e) => { if (S.get(bid) === s) { s.error = short(e); log(`binding ${bid}: listener error: ${short(e)}`); } });
  api.listener.start({ retryOnClose: true });
  log(`binding ${bid}: session live`);
}

function drop(bid) {
  const s = S.get(bid); if (!s) return;
  try { s.api?.listener?.stop(); } catch { /* already closed */ }
  S.delete(bid);
}

// ---- QR login: returns as soon as the first QR image exists; the login itself continues in the background
async function loginQr(bid) {
  drop(bid);
  const s = sess(bid), z = await client();
  s.status = "starting";
  return await new Promise((resolve) => {
    let first = true;
    const reply = () => { if (first) { first = false; resolve(view(bid)); } };
    z.loginQR({ userAgent: UA }, (ev) => {
      if (S.get(bid) !== s) return;
      if (ev.type === 0) { s.qr = String(ev.data.image).startsWith("data:") ? ev.data.image : "data:image/png;base64," + ev.data.image; s.status = "qr"; reply(); }
      else if (ev.type === 1) { s.qr = null; if (s.retries++ < 2) { s.status = "starting"; ev.actions.retry(); } else { s.status = "expired"; ev.actions.abort(); } }
      else if (ev.type === 2) { s.status = "scanned"; s.qr = null; s.name = String(ev.data?.display_name || ""); }
      else if (ev.type === 3) { s.status = "declined"; s.qr = null; }
      else if (ev.type === 4) {          // login blob -> backend (encrypted there); never logged here
        toBackend(bid, "session", { cookie: ev.data.cookie, imei: ev.data.imei, userAgent: ev.data.userAgent })
          .catch((e) => { s.error = "session not stored: " + short(e); log(`binding ${bid}: ${s.error}`); });
      }
    }).then((api) => { if (S.get(bid) === s) attach(bid, api); else api?.listener?.stop(); })
      .catch((e) => { if (S.get(bid) === s) { s.status = s.status === "expired" ? "expired" : "error"; s.error = short(e); s.qr = null; } reply(); });
    setTimeout(reply, 25000);
  });
}

async function startSession(bid, creds) {
  drop(bid);
  const s = sess(bid), z = await client();
  s.status = "starting";
  try { attach(bid, await z.login(creds)); }
  catch (e) { s.status = "error"; s.error = short(e); log(`binding ${bid}: login with stored session failed: ${s.error}`); }
  return view(bid);
}

// ---- http
const digest = (x) => createHash("sha256").update(String(x)).digest();
const authed = (req) => timingSafeEqual(digest(req.headers["x-zp-token"] || ""), digest(TOKEN));
const send = (res, code, obj) => { const b = JSON.stringify(obj); res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(b) }); res.end(b); };
async function readJson(req) {
  let n = 0; const parts = [];
  for await (const c of req) { n += c.length; if (n > MAX_BODY) throw Object.assign(new Error("too large"), { code: 413 }); parts.push(c); }
  if (!n) return {};
  try { const j = JSON.parse(Buffer.concat(parts).toString("utf8")); return j && typeof j === "object" ? j : {}; } catch { throw Object.assign(new Error("bad json"), { code: 400 }); }
}
const bidOf = (x) => { const n = Number(x?.binding_id); if (!Number.isInteger(n) || n < 1) throw Object.assign(new Error("binding_id required"), { code: 422 }); return n; };

const server = http.createServer(async (req, res) => {
  try {
    if (!authed(req)) return send(res, 401, { error: "bad token" });
    const url = new URL(req.url, "http://127.0.0.1");
    const route = `${req.method} ${url.pathname}`;
    if (route === "GET /status") {
      if (url.searchParams.get("binding_id")) return send(res, 200, view(Number(url.searchParams.get("binding_id"))));
      return send(res, 200, { ok: true, mock: MOCK, sessions: Object.fromEntries([...S].map(([k, v]) => [k, v.status])) });
    }
    if (req.method === "GET" && url.pathname === "/mock/sent" && MOCK) {
      return send(res, 200, { sent: zalo?.sent || [] });
    }
    if (req.method !== "POST") return send(res, 404, { error: "not found" });
    const body = await readJson(req);
    if (route === "POST /login/qr") return send(res, 200, await loginQr(bidOf(body)));
    if (route === "POST /session/start") {
      const c = body.session || {};
      if (!c.imei || !c.cookie || !c.userAgent) return send(res, 422, { error: "session needs cookie, imei, userAgent" });
      return send(res, 200, await startSession(bidOf(body), { cookie: c.cookie, imei: c.imei, userAgent: c.userAgent }));
    }
    if (route === "POST /session/stop") { drop(bidOf(body)); return send(res, 200, { ok: true }); }
    if (route === "POST /send") {
      const s = S.get(bidOf(body));
      if (!s?.api || s.status !== "logged_in") return send(res, 409, { error: "not logged in" });
      const text = String(body.text ?? ""), tid = String(body.thread_id ?? "");
      if (!tid || !text.trim() || text.length > 4000) return send(res, 422, { error: "thread_id and text (<= 4000 chars) required" });
      await s.api.sendMessage({ msg: text }, tid, body.type === "group" ? 1 : 0);   // exactly one thread, one message
      return send(res, 200, { ok: true });
    }
    if (MOCK && route === "POST /mock/scan") return send(res, 200, { ok: (await client()).scan() });
    if (MOCK && route === "POST /mock/incoming") {
      const bid = bidOf(body), s = S.get(bid);
      if (!s?.api || s.status !== "logged_in") return send(res, 409, { error: "not logged in" });
      const m = body.message || {};
      s.api.listener.emit("message", zcaShaped(s.ownId, m));
      return send(res, 200, { ok: true });
    }
    return send(res, 404, { error: "not found" });
  } catch (e) {
    return send(res, e.code >= 400 && e.code < 500 ? e.code : 500, { error: short(e) });
  }
});
server.listen(PORT, "127.0.0.1", () => log(`listening on 127.0.0.1:${PORT} (${MOCK ? "MOCK: fake zca client, no Zalo network" : "REAL zca-js 2.2.0, unofficial"})`));
if (process.env.ZP_WATCH_STDIN === "1") { process.stdin.resume(); process.stdin.on("end", () => process.exit(0)); process.stdin.on("error", () => process.exit(0)); }   // parent died -> exit
for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => process.exit(0));
