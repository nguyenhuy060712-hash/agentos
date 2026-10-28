"""Bảng giá model (app/pricing.json) + cập nhật từ LiteLLM (GĐ6.2, 22/10/2026). Không bao giờ tự áp dụng: refresh chỉ tạo đề xuất (diff), apply mới ghi file."""
import datetime as dt, json, os
from pathlib import Path

PATH = Path(__file__).with_name("pricing.json")
PENDING_PATH = Path(__file__).with_name("pricing_pending.json")
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
# our field (USD / 1M tokens) <- LiteLLM field (USD / token)
LITELLM_FIELDS = {"input_per_1m": "input_cost_per_token", "output_per_1m": "output_cost_per_token", "cached_input_per_1m": "cache_read_input_token_cost",
                  "cache_write_per_1m": "cache_creation_input_token_cost", "cache_write_1h_per_1m": "cache_creation_input_token_cost_above_1hr", "input_per_1m_long": "input_cost_per_token_above_200k_tokens",
                  "output_per_1m_long": "output_cost_per_token_above_200k_tokens",
                  "reasoning_per_1m": "output_cost_per_reasoning_token", "audio_input_per_1m": "input_cost_per_audio_token",
                  "audio_output_per_1m": "output_cost_per_audio_token", "image_input_per_1m": "input_cost_per_image_token",
                  "image_output_per_1m": "output_cost_per_image_token"}
PREFIXES = ("", "gemini/", "openai/", "vertex_ai/", "anthropic/")

def load(path=None):
    return json.loads(Path(path or PATH).read_text(encoding="utf-8"))

def save(data, path=None):
    p = Path(path or PATH); tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"); os.replace(tmp, p)

async def fetch_litellm():
    import httpx
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(LITELLM_URL); r.raise_for_status(); return r.json()

def _find(lit, model):
    """LiteLLM entry for one of our keys. Our canonical `provider/model` keys (GĐ11.3) are tried as-is first (LiteLLM also uses `deepseek/...`), then without the prefix."""
    names = [model] + ([model.split("/", 1)[1]] if "/" in model else [])
    for n in names:
        for p in PREFIXES:
            v = lit.get(p + n)
            if isinstance(v, dict): return v
    return None

def make_diff(models, lit):
    """{'changes': {model: {field: {'old','new'}}}, 'not_found': [...]} - only models we already list, only fields LiteLLM states and that differ."""
    changes, missing = {}, []
    for m, cur in models.items():
        e = _find(lit, m)
        if e is None: missing.append(m); continue
        ch = {}
        for f, lf in LITELLM_FIELDS.items():
            v = e.get(lf)
            if not isinstance(v, (int, float)) or isinstance(v, bool): continue
            new = round(v * 1e6, 6); old = cur.get(f)
            if old is None or abs(old - new) > 1e-9: ch[f] = {"old": old, "new": new}
        if ch: changes[m] = ch
    return {"changes": changes, "not_found": missing}

async def refresh(path=None, pending_path=None):
    lit = await fetch_litellm()
    d = make_diff(load(path)["models"], lit)
    d = {**d, "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "source": "litellm", "source_url": LITELLM_URL}
    Path(pending_path or PENDING_PATH).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d

def pending(pending_path=None):
    p = Path(pending_path or PENDING_PATH)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

def apply(path=None, pending_path=None):
    """Write the pending proposal into pricing.json (source=litellm + date). Returns the applied changes, or None if nothing is pending."""
    pp = Path(pending_path or PENDING_PATH); pd = pending(pp)
    if not pd: return None
    data = load(path); today = dt.date.today().isoformat(); n = 0
    for m, ch in pd["changes"].items():
        if m not in data["models"]: continue
        e = data["models"][m]
        for f, v in ch.items(): e[f] = v["new"]
        e.update(source="litellm", source_url=pd.get("source_url", LITELLM_URL), checked=today); n += 1
    save(data, path); pp.unlink()
    return {"applied_models": n, "changes": pd["changes"], "checked": today}
