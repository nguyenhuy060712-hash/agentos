// GĐ8.3b: in-process FAKE of the small part of zca-js the sidecar uses (ZALO_PERSONAL_MOCK=1). No network at all.
// Same surface as `new Zalo(...)`: loginQR(opts, cb) / login(credentials) -> api {listener, sendMessage, getOwnId}.
// Tests drive it through the sidecar's /mock/* endpoints: scan() finishes the QR login, inject(msg) delivers a zca-shaped message.
import { EventEmitter } from "node:events";
import { randomBytes } from "node:crypto";
import { deflateSync } from "node:zlib";

// A 29x29-module chequer-board PNG with 3 finder squares, shown by the UI as the "QR" (a real QR comes from Zalo in real mode).
function fakeQrPng() {
  const N = 29, S = 8, W = N * S;
  const raw = Buffer.alloc((W * 3 + 1) * W);
  let seed = 7;
  const grid = Array.from({ length: N }, (_, y) => Array.from({ length: N }, (_, x) => {   // modules are computed ONCE (a per-pixel generator would give noise)
    for (const [cx, cy] of [[0, 0], [N - 7, 0], [0, N - 7]]) {
      const dx = x - cx, dy = y - cy;
      if (dx >= -1 && dx <= 7 && dy >= -1 && dy <= 7) return dx >= 0 && dy >= 0 && dx < 7 && dy < 7 && (dx === 0 || dy === 0 || dx === 6 || dy === 6 || (dx >= 2 && dx <= 4 && dy >= 2 && dy <= 4));
    }
    seed = (seed * 1103515245 + 12345) & 0x7fffffff; return (seed >> 16) & 1;
  }));
  for (let y = 0; y < W; y++) {
    const row = y * (W * 3 + 1); raw[row] = 0;
    for (let x = 0; x < W; x++) { const v = grid[Math.floor(y / S)][Math.floor(x / S)] ? 0 : 255; raw[row + 1 + x * 3] = raw[row + 2 + x * 3] = raw[row + 3 + x * 3] = v; }
  }
  return encodePng(W, W, raw);
}
function crc32(buf) { let c, crc = ~0; for (let n = 0; n < buf.length; n++) { c = (crc ^ buf[n]) & 0xff; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; crc = (crc >>> 8) ^ c; } return ~crc >>> 0; }
function chunk(type, data) { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td)); return Buffer.concat([len, td, crc]); }
function encodePng(w, h, raw) {
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}

class FakeListener extends EventEmitter {
  start() { this.running = true; queueMicrotask(() => this.emit("connected")); }
  stop() { this.running = false; }
}

class FakeApi {
  constructor(ownId, sent) { this.listener = new FakeListener(); this.ownId = ownId; this.sent = sent; }
  getOwnId() { return this.ownId; }
  async sendMessage(message, threadId, type) {
    const msg = typeof message === "string" ? message : message.msg;
    this.sent.push({ thread_id: String(threadId), type: type === 1 ? "group" : "user", text: msg, at: Date.now() });
    return { message: { msgId: Date.now() }, attachment: [] };
  }
}

export class MockZalo {
  constructor() { this.sent = []; this.pending = new Map(); }

  // QR login: emits a QRCodeGenerated event (type 0) and then waits for scan() (-> GotLoginInfo type 4, resolve api).
  loginQR(options, cb) {
    return new Promise((resolve, reject) => {
      const token = randomBytes(6).toString("hex");
      const done = () => {
        const cookie = [{ key: "zpsid", value: "MOCKCOOKIE-" + randomBytes(12).toString("hex"), domain: "chat.zalo.me", path: "/" }];
        const creds = { cookie, imei: "mock-imei-" + randomBytes(4).toString("hex"), userAgent: options?.userAgent || "mock-agent" };
        cb({ type: 2, data: { avatar: "", display_name: "Số phụ (thử)" }, actions: { retry() {}, abort() {} } });
        cb({ type: 4, data: creds, actions: null });
        resolve(new FakeApi("mock-own-" + token, this.sent));
      };
      this.pending.set(token, { done, reject });
      this.lastToken = token;
      cb({ type: 0, data: { code: token, image: fakeQrPng().toString("base64"), options: {}, token },
           actions: { saveToFile: async () => {}, retry() {}, abort: () => { this.pending.delete(token); reject(new Error("aborted")); } } });
    });
  }

  async login(credentials) {
    if (!credentials?.imei || !credentials?.cookie) throw new Error("bad credentials");
    return new FakeApi("mock-own-" + String(credentials.imei).slice(-8), this.sent);
  }

  scan() { const p = this.pending.get(this.lastToken); if (!p) return false; this.pending.delete(this.lastToken); p.done(); return true; }
}

// A zca-shaped incoming message (UserMessage / GroupMessage) built from the simple test input.
export function zcaShaped(ownId, m) {
  const group = !!m.group;
  const ts = String(Date.now());
  const base = { msgId: m.msg_id || randomBytes(6).toString("hex"), cliMsgId: "1", uidFrom: String(m.sender_id), idTo: group ? String(m.thread_id) : ownId,
                 dName: m.sender_name || "", ts, msgType: "webchat", content: m.text ?? "", ttl: 0 };
  if (m.file) {
    const isImg = m.file.kind === "image";
    base.msgType = isImg ? "chat.photo" : "share.file";
    base.content = { title: m.file.name || (isImg ? "image.jpg" : "file"), description: m.text || "", href: m.file.url, thumb: "",
                     params: JSON.stringify({ fileSize: String(m.file.size ?? "") }), type: "", action: "", childnumber: 0 };
  }
  if (group) {
    const own = m.mention_self ? [{ uid: ownId, pos: 0, len: m.mention_len ?? 0 }] : [];
    return { type: 1, threadId: String(m.thread_id), isSelf: false, data: { ...base, mentions: [...own, ...(m.mentions || [])] } };
  }
  return { type: 0, threadId: String(m.thread_id || m.sender_id), isSelf: false, data: base };
}
