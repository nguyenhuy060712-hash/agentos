"""GD7.5: pure helpers of judge alignment (no DB / server imports, unit-testable): agreement metrics, diverse pick of disagreements, few-shot example shapes."""
import math, re
from collections import Counter

def cohens_kappa(pairs):
    """pairs: [(a, b)] labels from two raters. kappa = (po - pe) / (1 - pe); None when undefined (pe = 1, i.e. both raters used one single label)."""
    n = len(pairs)
    if not n: return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return None if pe >= 1 else round((po - pe) / (1 - pe), 4)

def pearson(xs, ys):
    n = len(xs)
    if n < 2: return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if sx == 0 or sy == 0 else round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 4)

def mode_of(pairs):
    vals = [v for p in pairs for v in (p["human"], p["judge"])]
    if any(isinstance(v, str) for v in vals): return "categorical"
    return "categorical" if all(v in (0, 1) for v in vals) else "continuous"

def unit(v, lo, hi): return 0.0 if hi == lo else (float(v) - lo) / (hi - lo)

def human_range(pairs, scale):
    """Range used to normalise the human side: the judge's scale when every human value fits in it, else the observed range (e.g. 1-5 labels vs a 0-1 judge)."""
    lo, hi = scale; hs = [float(p["human"]) for p in pairs]
    return (lo, hi) if all(lo <= h <= hi for h in hs) else (min(hs), max(hs))

def metrics(pairs, scale=(0.0, 1.0)):
    """pairs: [{human, judge}] (floats or strings). {mode, n, agreement, accuracy, kappa, correlation, mae}; agreement = accuracy (categorical) or 1 - normalised MAE (continuous)."""
    n = len(pairs)
    if not n: return {"mode": None, "n": 0, "agreement": None, "accuracy": None, "kappa": None, "correlation": None, "mae": None}
    mode = mode_of(pairs)
    if mode == "categorical":
        acc = round(sum(1 for p in pairs if p["human"] == p["judge"]) / n, 4)
        return {"mode": mode, "n": n, "agreement": acc, "accuracy": acc, "kappa": cohens_kappa([(p["human"], p["judge"]) for p in pairs]), "correlation": None, "mae": None}
    lo, hi = scale; hl, hh = human_range(pairs, scale)
    hn, jn = [unit(p["human"], hl, hh) for p in pairs], [unit(p["judge"], lo, hi) for p in pairs]
    mae = sum(abs(a - b) for a, b in zip(hn, jn)) / n
    return {"mode": mode, "n": n, "agreement": round(1 - mae, 4), "accuracy": None, "kappa": None, "correlation": pearson(hn, jn), "mae": round(mae, 4)}

def disagrees(p, mode, scale=(0.0, 1.0), hrange=None):
    if mode == "categorical": return p["human"] != p["judge"]
    lo, hi = scale; hl, hh = hrange or (lo, hi)
    return abs(unit(p["human"], hl, hh) - unit(p["judge"], lo, hi)) >= 0.25

# ---- quick align: which disagreements become few-shot examples
_TOK = re.compile(r"\w+", re.U)
def _words(p): return set(_TOK.findall((p["input"] + " " + p["output"]).lower()))

def pick_diverse(dis, k):
    """k disagreements: start from the most recent, then repeatedly the one least similar (word overlap, Jaccard) to everything picked; ties -> more recent."""
    cand = sorted(dis, key=lambda p: -p["fid"]); k = min(k, len(cand))
    if not k: return []
    picked = [cand.pop(0)]
    while len(picked) < k and cand:
        sim = lambda c: max((len(_words(c) & _words(q)) / (len(_words(c) | _words(q)) or 1)) for q in picked)
        best = min(range(len(cand)), key=lambda i: (round(sim(cand[i]), 6), i))
        picked.append(cand.pop(best))
    return picked

def as_judge_type(ev, v):
    """Human label -> the value type the judge uses (bool for a boolean openevals judge, 0-1 float for a continuous one, raw otherwise)."""
    if isinstance(v, str): return v
    if ev["kind"] == "openevals": return round(max(0.0, min(1.0, float(v))), 4) if ev["config"].get("continuous") else float(v) >= 0.5
    return v

def example(ev, p, view_in, view_out):
    """few-shot example for the judge. openevals kind: openevals FewShotExample {inputs, outputs, score, reasoning}; llm kind: the dict shape the use_corrections few-shot already uses."""
    h = p["human"]; note = (p["comment"] or "").strip()
    if ev["kind"] == "openevals":
        why = f"Human reviewers rated this {h!r} (the judge had said {p['judge']!r})." + (f" Reviewer note: {note}" if note else "")
        return {"inputs": view_in, "outputs": view_out, "score": as_judge_type(ev, h), "reasoning": why, "_align": True}
    return {"input": view_in[:300], "output": view_out[:300], "điểm_giám_khảo_cũ": p["judge"], "điểm_đúng_do_người_sửa": h, "ghi_chú": note or None, "_align": True}
