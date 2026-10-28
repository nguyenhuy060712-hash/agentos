"""Plugin pytest cho Moza Agent OS: mỗi test = 1 ví dụ; cuối phiên gửi thành experiment (dataset tự tạo, có phiên bản).
Dùng:  conftest.py →  pytest_plugins = ["agentos_pytest"]
       AGENTOS_ENDPOINT=... AGENTOS_API_KEY=ak_... AGENTOS_DATASET=ten_dataset [AGENTOS_EXPERIMENT=ten] pytest
Trong test:
    @pytest.mark.agentos
    def test_gio(agentos):
        agentos.log_inputs({"question": "Mấy giờ rồi?"})
        agentos.log_reference({"answer": "có giờ"})
        out = goi_agent(...)
        agentos.log_outputs({"answer": out})
        agentos.log_feedback("co_chu_so", any(c.isdigit() for c in out))
        agentos.expect.value(out).to_contain("giờ")                       # logged as feedback `expectation` (1/0)
        agentos.expect.edit_distance(out, "có giờ").to_be_less_than(0.5)   # logged as `edit_distance`; also expect.embedding_distance
        assert out
Pass/fail → feedback "pass" (1/0); thời gian chạy → latency. Không có AGENTOS_API_KEY thì plugin im lặng (không gửi)."""
import os, time
import pytest
import agentos_sdk as ao
import agentos_evals as ev   # trajectory_match / composite: same logic as the server evaluators

class _Rec:
    def __init__(self, nodeid):
        self.d = {"test_name": nodeid, "inputs": None, "outputs": None, "reference": None, "feedback": {}}; self._multi = {}
    def log_inputs(self, x): self.d["inputs"] = x
    def log_outputs(self, x): self.d["outputs"] = x
    def log_reference(self, x): self.d["reference"] = x
    def log_feedback(self, key, score): self.d["feedback"][key] = score
    def log_trajectory(self, calls):
        """calls: [{"name": tool, "args": {...}}, ...] or tool names; stored as child tool runs in the experiment."""
        self.d["trajectory"] = [c if isinstance(c, dict) else {"name": c} for c in calls]
    def trajectory_match(self, reference, mode="strict", args="ignore", key=None):
        """Score the logged trajectory against a reference (strict|unordered|subset|superset); logs feedback and returns the score."""
        r = ev.trajectory_match(self.d.get("trajectory") or [], reference, mode, args)
        self.log_feedback(key or f"trajectory_{mode}", r["score"]); return r["score"]
    def composite(self, key, keys=None, weights=None, aggregate="weighted_mean", formula=None):
        """Combine already-logged feedback scores into one (average|weighted_mean|sum|min|max|all_pass|formula, e.g. formula="0.7*correct + 0.3*tone")."""
        fb = {k: v for k, v in self.d["feedback"].items() if keys is None or k in keys}
        fb = {k: (1.0 if v else 0.0) if isinstance(v, bool) else v for k, v in fb.items() if isinstance(v, (int, float, bool))}
        sc = ev.composite(fb, weights, aggregate, formula=formula); self.log_feedback(key, sc); return sc
    @property
    def expect(self):
        """LangSmith-style assertions that are also logged as feedback: expect.value(x).to_contain(y), expect.edit_distance(a, b).to_be_less_than(0.3), expect.embedding_distance(a, b)..."""
        if "_expect" not in self.__dict__:
            def sink(key, score, comment):   # several results under one key (e.g. many `expectation`) are stored as their mean
                self._multi.setdefault(key, []).append(score); v = self._multi[key]; self.d["feedback"][key] = round(sum(v) / len(v), 4)
            self.__dict__["_expect"] = ev.Expect(sink)
        return self.__dict__["_expect"]

_RESULTS, _RECS = [], {}

def pytest_configure(config):
    config.addinivalue_line("markers", "agentos: ghi test này thành ví dụ trong experiment Agent OS")

@pytest.fixture
def agentos(request):
    r = _RECS.setdefault(request.node.nodeid, _Rec(request.node.nodeid)); return r

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when != "call": return
    if not (item.get_closest_marker("agentos") or item.nodeid in _RECS): return
    r = _RECS.get(item.nodeid) or _Rec(item.nodeid)
    d = dict(r.d); d["inputs"] = d["inputs"] or {"test": item.nodeid}
    d["feedback"] = {**d["feedback"], "pass": rep.passed}
    d["latency_ms"] = int(rep.duration * 1000)
    if rep.failed: d["error"] = str(rep.longrepr)[-1500:]
    _RESULTS.append(d)

def pytest_sessionfinish(session, exitstatus):
    if not _RESULTS or not os.environ.get("AGENTOS_API_KEY"): return
    ao.configure(endpoint=os.environ.get("AGENTOS_ENDPOINT"), api_key=os.environ["AGENTOS_API_KEY"])
    ds = os.environ.get("AGENTOS_DATASET", "pytest")
    name = os.environ.get("AGENTOS_EXPERIMENT") or f"pytest {time.strftime('%d/%m %H:%M')}"
    try:
        r = ao.log_experiment(ds, name, _RESULTS, {"source": "pytest", "exit_status": int(exitstatus)})
        print(f"\n[agentos] experiment #{r['experiment_id']} ({len(_RESULTS)} test) → dataset '{ds}' v{r['dataset_version']}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[agentos] gửi experiment lỗi: {e}")
