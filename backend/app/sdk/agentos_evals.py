"""Pure-Python evaluator helpers shared by the Agent OS server and the SDK (pytest). No third-party deps.
trajectory_match modes (same semantics as the public agentevals docs):
  strict    – same tool calls in the same order
  unordered – same tool calls, any order (multiset equality)
  subset    – every call the agent made appears in the reference (agent did nothing extra)
  superset  – every reference call appears in the agent run (agent did at least the reference)
args: "ignore" (names only) | "exact" (names + args equal) | "subset" (reference args ⊆ run args)."""
from collections import Counter
import json

MODES = ("strict", "unordered", "subset", "superset")

def _norm_traj(t):
    out = []
    for x in t or []:
        if isinstance(x, str): out.append({"name": x, "args": None})
        elif isinstance(x, dict): out.append({"name": str(x.get("name") or x.get("tool") or ""), "args": x.get("args", x.get("arguments"))})
    return [x for x in out if x["name"] and x["name"] != "write_todos"]

def _args_ok(run_args, ref_args, how):
    if how == "ignore" or ref_args is None: return True
    if isinstance(run_args, str):
        try: run_args = json.loads(run_args)
        except Exception: pass  # noqa: BLE001
    if how == "exact": return run_args == ref_args
    if isinstance(ref_args, dict) and isinstance(run_args, dict): return all(run_args.get(k) == v for k, v in ref_args.items())
    return run_args == ref_args

def _key(x, how): return x["name"] if how == "ignore" or x["args"] is None else x["name"] + json.dumps(x["args"], sort_keys=True, default=str)

def _contained(small, big, how):
    """every call in `small` can be matched to a distinct call in `big`"""
    used = [False] * len(big)
    for s in small:
        for i, b in enumerate(big):
            if not used[i] and b["name"] == s["name"] and _args_ok(b["args"], s["args"], how):
                used[i] = True; break
        else:
            return False, s
    return True, None

def trajectory_match(run_traj, ref_traj, mode="strict", args="ignore"):
    """Returns {"score": 1.0|0.0, "comment": str}."""
    if mode not in MODES: raise ValueError(f"mode must be one of {MODES}")
    r, f = _norm_traj(run_traj), _norm_traj(ref_traj)
    names = lambda t: [x["name"] for x in t]
    if mode == "strict":
        ok = len(r) == len(f) and all(a["name"] == b["name"] and _args_ok(a["args"], b["args"], args) for a, b in zip(r, f))
        why = "" if ok else f"expected {names(f)} got {names(r)}"
    elif mode == "unordered":
        ok = len(r) == len(f) and _contained(f, r, args)[0]
        why = "" if ok else f"expected (any order) {sorted(names(f))} got {sorted(names(r))}"
    elif mode == "subset":
        ok, miss = _contained(r, f, args)
        why = "" if ok else f"extra call not in reference: {miss['name']}"
    else:
        ok, miss = _contained(f, r, args)
        why = "" if ok else f"missing reference call: {miss['name']}"
    return {"score": 1.0 if ok else 0.0, "comment": f"trajectory {mode}: " + ("match" if ok else why)}

# ---------------------------------------------------------------- GD9.Q (same code in app/evalkit.py and app/sdk/agentos_evals.py - keep both in sync)
import ast as _ast, hashlib as _hl, math as _math, re as _re

def edit_distance(a, b, limit=20000):
    """Levenshtein distance (insert / delete / substitute = 1)."""
    a, b = str(a)[:limit], str(b)[:limit]
    if a == b: return 0
    if not a or not b: return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1): cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def normalized_edit_distance(a, b):
    """0.0 = identical ... 1.0 = nothing in common (edit distance / longer length)."""
    n = max(len(str(a)), len(str(b)))
    return 0.0 if not n else round(edit_distance(a, b) / n, 4)

def hash_embed(texts, dims=256):
    """Offline embedding for tests / the SDK: hashed word + character-trigram counts, unit length. Pass your own `embed` for real semantics."""
    out = []
    for t in texts:
        s = " " + _re.sub(r"\s+", " ", str(t).lower()).strip() + " "; v = [0.0] * dims
        for g in [s[i:i + 3] for i in range(max(1, len(s) - 2))] + s.split():
            v[int.from_bytes(_hl.blake2b(g.encode(), digest_size=4).digest(), "big") % dims] += 1.0
        n = _math.sqrt(sum(x * x for x in v)) or 1.0; out.append([x / n for x in v])
    return out

def cosine_distance(u, v):
    nu, nv = _math.sqrt(sum(x * x for x in u)) or 1.0, _math.sqrt(sum(x * x for x in v)) or 1.0
    return round(max(0.0, 1.0 - sum(x * y for x, y in zip(u, v)) / (nu * nv)), 4)

def embedding_distance(a, b, embed=None):
    """Cosine distance between the embeddings of two texts (0 = same meaning). embed(list[str]) -> list[vector]."""
    va, vb = (embed or hash_embed)([str(a), str(b)])[:2]
    return cosine_distance(va, vb)

_F_FUNCS = {"min": min, "max": max, "sum": lambda *a: sum(a), "avg": lambda *a: sum(a) / len(a), "abs": abs, "round": round,
            "sqrt": _math.sqrt}
_F_NODES = (_ast.Expression, _ast.BinOp, _ast.UnaryOp, _ast.Constant, _ast.Name, _ast.Load, _ast.Call, _ast.IfExp, _ast.Compare, _ast.BoolOp,
            _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.Mod, _ast.USub, _ast.UAdd, _ast.Not, _ast.And, _ast.Or, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
            _ast.Eq, _ast.NotEq, _ast.Subscript)

def var_name(key):
    """feedback key -> identifier usable in a formula (non-alphanumerics become _; also reachable as s["key"])."""
    return _re.sub(r"\W", "_", str(key))

def safe_formula(expr, scores):
    """Evaluate a custom composite formula over feedback scores, e.g. "0.7*correctness + 0.3*min(tone, formatting)". Only arithmetic, comparisons,
    if-else, min/max/sum/avg/abs/round/sqrt and the score names - no attribute access, no imports, no other calls. Raises ValueError."""
    expr = str(expr or "").strip()
    if not expr or len(expr) > 400: raise ValueError("formula must be 1-400 characters")
    try: tree = _ast.parse(expr, mode="eval")
    except SyntaxError as e: raise ValueError(f"formula syntax error: {e.msg}") from None
    env = {var_name(k): float(v) for k, v in scores.items()}
    for n in _ast.walk(tree):
        if not isinstance(n, _F_NODES): raise ValueError(f"formula: {type(n).__name__} is not allowed")
        if isinstance(n, _ast.Call) and not (isinstance(n.func, _ast.Name) and n.func.id in _F_FUNCS and not n.keywords): raise ValueError("formula: only min/max/sum/avg/abs/round/sqrt may be called")
        if isinstance(n, _ast.Name) and n.id not in env and n.id not in _F_FUNCS and n.id != "s": raise ValueError(f"formula: unknown score '{n.id}' (known: {', '.join(sorted(env)) or 'none'})")
        if isinstance(n, _ast.Constant) and not isinstance(n.value, (int, float, str, bool)): raise ValueError("formula: bad constant")
    def ev(n):
        if isinstance(n, _ast.Expression): return ev(n.body)
        if isinstance(n, _ast.Constant): return n.value
        if isinstance(n, _ast.Name): return env[n.id] if n.id in env else _F_FUNCS[n.id]
        if isinstance(n, _ast.Subscript):
            sl = n.slice
            if not (isinstance(n.value, _ast.Name) and n.value.id == "s" and isinstance(sl, _ast.Constant)): raise ValueError('formula: use s["key"]')
            if sl.value not in scores: raise ValueError(f"formula: unknown score '{sl.value}'")
            return float(scores[sl.value])
        if isinstance(n, _ast.UnaryOp):
            v = ev(n.operand); return -v if isinstance(n.op, _ast.USub) else (not v if isinstance(n.op, _ast.Not) else v)
        if isinstance(n, _ast.BinOp):
            l, r = ev(n.left), ev(n.right)
            return {_ast.Add: lambda: l + r, _ast.Sub: lambda: l - r, _ast.Mult: lambda: l * r, _ast.Div: lambda: l / r, _ast.Mod: lambda: l % r}[type(n.op)]()
        if isinstance(n, _ast.IfExp): return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, _ast.BoolOp):
            vals = [ev(x) for x in n.values]; return all(vals) if isinstance(n.op, _ast.And) else any(vals)
        if isinstance(n, _ast.Compare):
            l = ev(n.left)
            for op, c in zip(n.ops, n.comparators):
                r = ev(c)
                if not {_ast.Lt: l < r, _ast.LtE: l <= r, _ast.Gt: l > r, _ast.GtE: l >= r, _ast.Eq: l == r, _ast.NotEq: l != r}[type(op)]: return False
                l = r
            return True
        if isinstance(n, _ast.Call): return _F_FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("formula: unsupported expression")
    try: v = ev(tree)
    except ZeroDivisionError: raise ValueError("formula: division by zero") from None
    except (TypeError, OverflowError) as e: raise ValueError(f"formula: {e}") from None
    return float(v)

AGGREGATES = ("weighted_mean", "average", "sum", "min", "max", "all_pass", "formula")

def composite(scores, weights=None, aggregate="weighted_mean", pass_threshold=1.0, formula=None):
    """scores: {feedback key: float}. aggregate: average (= weighted_mean: sum(w*s)/sum(w), LangSmith "Average") | sum (sum(w*s), LangSmith "Sum")
    | min | max | all_pass (1 when every score >= pass_threshold) | formula (custom expression over the keys). Returns float | None."""
    vals = {k: (1.0 if v is True else 0.0 if v is False else float(v)) for k, v in scores.items() if v is not None}
    if not vals: return None
    if aggregate == "min": return min(vals.values())
    if aggregate == "max": return max(vals.values())
    if aggregate == "all_pass": return 1.0 if all(v >= pass_threshold for v in vals.values()) else 0.0
    if aggregate == "formula": return round(safe_formula(formula, vals), 4)
    w = {str(k): float(v) for k, v in (weights or {}).items()}
    if aggregate == "sum": return round(sum(v * w.get(k, 1.0) for k, v in vals.items()), 4)
    tw = sum(w.get(k, 1.0) for k in vals)
    return round(sum(v * w.get(k, 1.0) for k, v in vals.items()) / (tw or 1), 4)

# ---------------------------------------------------------------- GD9.Q `expect` (LangSmith pytest-integration assertions, logged as feedback)
class _Score:
    """Number returned by expect.edit_distance / embedding_distance / score: assert on it (to_be_less_than ...) or read .value."""
    def __init__(self, ex, key, value): self._ex, self.key, self.value = ex, key, value
    def __float__(self): return float(self.value)
    def __repr__(self): return f"<{self.key} {self.value}>"
    def _chk(self, ok, msg): return self._ex._expectation(ok, msg)
    def to_be_less_than(self, x): return self._chk(self.value < x, f"{self.key}={self.value} is not less than {x}")
    def to_be_less_than_or_equal(self, x): return self._chk(self.value <= x, f"{self.key}={self.value} is not <= {x}")
    def to_be_greater_than(self, x): return self._chk(self.value > x, f"{self.key}={self.value} is not greater than {x}")
    def to_be_between(self, lo, hi): return self._chk(lo <= self.value <= hi, f"{self.key}={self.value} is not between {lo} and {hi}")

class _Value:
    """expect.value(x).to_equal / to_contain / to_match / to_be_less_than / to_satisfy: logs `expectation` (1 or 0) then raises AssertionError when false."""
    def __init__(self, ex, v): self._ex, self.v = ex, v
    def _chk(self, ok, msg): return self._ex._expectation(ok, msg)
    def to_equal(self, other): return self._chk(self.v == other, f"{self.v!r} != {other!r}")
    def to_contain(self, other): return self._chk(other in self.v, f"{other!r} not in {str(self.v)[:200]!r}")
    def to_not_contain(self, other): return self._chk(other not in self.v, f"{other!r} unexpectedly in {str(self.v)[:200]!r}")
    def to_match(self, pattern): return self._chk(_re.search(pattern, str(self.v)) is not None, f"{pattern!r} does not match {str(self.v)[:200]!r}")
    def to_be_less_than(self, x): return self._chk(self.v < x, f"{self.v!r} is not less than {x!r}")
    def to_be_greater_than(self, x): return self._chk(self.v > x, f"{self.v!r} is not greater than {x!r}")
    def to_satisfy(self, fn, name="custom"):
        """custom assertion: fn(value) -> truthy"""
        try: ok = bool(fn(self.v))
        except Exception as e: return self._chk(False, f"{name} raised {type(e).__name__}: {e}")  # noqa: BLE001
        return self._chk(ok, f"{name} is not satisfied by {str(self.v)[:200]!r}")

class Expect:
    """LangSmith `expect` for tests and scripts. sink(key, score, comment) receives every result (the pytest plugin / ao.expect_for_run turn them into feedback):
        expect.value(answer).to_contain("Paris")                  -> feedback `expectation` 1/0
        expect.edit_distance(answer, reference).to_be_less_than(0.3)  -> feedback `edit_distance` (normalized 0..1) + `expectation`
        expect.embedding_distance(answer, reference, config={"embed": my_embed}).to_be_less_than(0.4)   -> feedback `embedding_distance` + `expectation`
        expect.score(0.8, key="tone")                             -> feedback `tone`"""
    def __init__(self, sink=None, embed=None): self.sink, self.embed, self.results = sink, embed, []
    def _log(self, key, score, comment=None):
        self.results.append({"key": key, "score": score, "comment": comment})
        if self.sink: self.sink(key, score, comment)
    def _expectation(self, ok, msg):
        self._log("expectation", 1.0 if ok else 0.0, None if ok else msg)
        if not ok: raise AssertionError(msg)
        return True
    def value(self, v): return _Value(self, v)
    def score(self, value, key="score"): self._log(key, float(value)); return _Score(self, key, float(value))
    def edit_distance(self, prediction, reference, config=None):
        d = normalized_edit_distance(prediction, reference); self._log("edit_distance", d); return _Score(self, "edit_distance", d)
    def embedding_distance(self, prediction, reference, config=None):
        d = embedding_distance(prediction, reference, (config or {}).get("embed") or self.embed); self._log("embedding_distance", d); return _Score(self, "embedding_distance", d)

expect = Expect()   # module-level instance without a sink (use Expect(sink) or the pytest `agentos` fixture to log)
