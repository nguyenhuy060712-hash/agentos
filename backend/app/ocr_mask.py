"""GĐ9.P2 PII inside images: local OCR (RapidOCR 3.x, onnxruntime, Apache-2.0, no network at run time) + the VN recognizers of pii.py.
When a workspace has PII masking on, an image the user attached is OCR'd; every region that contains personal data is painted over with a black box on a COPY
that goes to the model (the original stays in chat_files, encrypted at rest when the workspace enabled it). If OCR is unavailable or fails, the image is not sent
(the caller puts a short note instead): personal data is never leaked because of a tooling problem.
Lazy: nothing is imported or loaded until the first image of a masking workspace; the engine is released after IDLE_S seconds without use (RAM ~ +300 MB while loaded)."""
from __future__ import annotations
import io, logging, os, threading, time, unicodedata

log = logging.getLogger("aos.ocr")
IDLE_S = float(os.environ.get("AOS_OCR_IDLE_S", "600"))
MAX_SIDE = 2048          # larger photos are analysed at this size (boxes are scaled back); 9.Z: bounds OCR RAM on the VPS
PAD = 3                  # px around a masked region
_LOCK = threading.Lock()
_RUN = threading.Lock()   # 9.Z: one OCR at a time (peak RSS of parallel 12 MP OCRs added up to ~1 GB)
_ENG: dict = {"e": None, "t": 0.0}

def available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False

def _engine():
    with _LOCK:
        if _ENG["e"] is None:
            from rapidocr import RapidOCR
            _ENG["e"] = RapidOCR(params={"Global.log_level": "error"})
        _ENG["t"] = time.monotonic(); t = threading.Timer(IDLE_S + 1, release_if_idle); t.daemon = True; t.start(); return _ENG["e"]

def release_if_idle(now=None):
    with _LOCK:
        if _ENG["e"] is not None and (now or time.monotonic()) - _ENG["t"] > IDLE_S: _ENG["e"] = None; return True
    return False

def _regions(lines, text_spans, findings):
    """findings (char ranges in the joined text) -> pixel rectangles. Each line box is split proportionally to the characters that matched."""
    rects = []
    for f in findings:
        for (a, b), box in zip(text_spans, lines):
            lo, hi = max(f.start, a), min(f.end, b)
            if lo >= hi: continue
            xs = [p[0] for p in box]; ys = [p[1] for p in box]; x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys); n = max(1, b - a)
            rects.append((x0 + (x1 - x0) * (lo - a) / n, y0, x0 + (x1 - x0) * (hi - a) / n, y1))
    return rects

def mask_image(data: bytes, cfg: dict | None = None):
    """-> (bytes of the masked copy, [finding tags], mime of the copy or None when unchanged) or None when the image cannot be checked (OCR missing / failed / not an image). No findings -> (data, [])."""
    import pii
    from PIL import Image, ImageDraw
    try:
        im = Image.open(io.BytesIO(data)); fmt = im.format or "PNG"
        if fmt == "JPEG": im.draft("RGB", (MAX_SIDE, MAX_SIDE))   # 9.Z: decode big JPEGs already reduced (DCT scaling) -> far less RAM
        im.load()
    except Exception:  # noqa: BLE001
        return None
    try:
        work = im.convert("RGB"); scale = 1.0
        if max(work.size) > MAX_SIDE: scale = MAX_SIDE / max(work.size); work = work.resize((int(work.width * scale), int(work.height * scale)))
        import numpy as np
        with _RUN: res = _engine()(np.array(work)[:, :, ::-1])
        txts, boxes = list(getattr(res, "txts", None) or ()), list(getattr(res, "boxes", None) if getattr(res, "boxes", None) is not None else ())
    except Exception as e:  # noqa: BLE001
        log.warning("OCR failed (%s)", type(e).__name__); return None
    if not txts: return data, [], None
    txts = [unicodedata.normalize("NFC", t) for t in txts]; spans, pos = [], 0
    for t in txts: spans.append((pos, pos + len(t))); pos += len(t) + 1
    cfg = cfg or {}
    fs = pii.analyze("\n".join(txts), cfg.get("entities"), float(cfg.get("threshold") or 0.5), bool(cfg.get("ner")))
    if not fs: return data, [], None
    rects = _regions([[(float(p[0]), float(p[1])) for p in b] for b in boxes], spans, fs)
    out = im.convert("RGB").copy(); d = ImageDraw.Draw(out); k = 1 / scale
    for x0, y0, x1, y1 in rects: d.rectangle((x0 * k - PAD, y0 * k - PAD, x1 * k + PAD, y1 * k + PAD), fill="black")
    buf = io.BytesIO()
    if fmt == "JPEG": out.save(buf, "JPEG", quality=92)
    else: out.save(buf, "PNG")
    return buf.getvalue(), sorted({f.tag for f in fs}), "image/jpeg" if fmt == "JPEG" else "image/png"
