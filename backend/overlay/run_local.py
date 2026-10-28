"""LOCAL-ONLY launcher for the real Agent OS server.py (copied from the #83 mirror) on the box.
- Postgres 17 + pgvector on 127.0.0.1:5432 (local, throwaway)
- every LLM / embedding / judge call is replaced by deterministic fakes -> no network, no spend
- OpenAI/Google endpoints are pointed at a black-hole address as a second safety net."""
import os, re, sys, hashlib
HERE = os.path.dirname(os.path.abspath(__file__)); APP = os.path.join(os.path.dirname(HERE), "app")
sys.path[:0] = [HERE, APP]; os.chdir(APP)
os.environ.update({
    "PG_URI": os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"),
    "GOOGLE_API_KEY": "local-fake-no-calls", "HA_URL": "http://127.0.0.1:9", "HA_TOKEN": "x",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1", "GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:9",
    "AGENTOS_SBX_DIR": "/workspace/agentos-local/var/sandboxes", "AGENTOS_EXPORT_DIR": "/workspace/agentos-local/var/exports",
    "AOS_MOCK_OAUTH": "0", "AOS_WORKER": "api",
    # GĐ8.2: Telegram only against a local fake Bot API (tests/test_gd8_telegram.py), never api.telegram.org
    "TELEGRAM_MOCK": "1", "TELEGRAM_API_BASE": os.environ.get("TELEGRAM_API_BASE", "http://127.0.0.1:8781"),
    "TELEGRAM_SUPERVISOR_S": os.environ.get("TELEGRAM_SUPERVISOR_S", "2"), "TELEGRAM_POLL_TIMEOUT": os.environ.get("TELEGRAM_POLL_TIMEOUT", "2"),
    # GĐ8.3: Zalo OA + OAuth only against a local fake (tests/test_gd8_zalo.py), never openapi.zalo.me / oauth.zaloapp.com
    "ZALO_MOCK": "1", "ZALO_API_BASE": os.environ.get("ZALO_API_BASE", "http://127.0.0.1:8782"), "ZALO_OAUTH_BASE": os.environ.get("ZALO_OAUTH_BASE", "http://127.0.0.1:8782"),
    # GĐ8.3b: Zalo PERSONAL (unofficial) -> the sidecar only ever runs its FAKE zca client here (no Zalo network, no real QR); feature switched on for the local stack
    "ZALO_PERSONAL_MOCK": "1", "AOS_ZALO_PERSONAL": os.environ.get("AOS_ZALO_PERSONAL", "1"), "ZALO_PERSONAL_SUPERVISOR_S": os.environ.get("ZALO_PERSONAL_SUPERVISOR_S", "2"),
    # GĐ8.4: Slack Web API + Socket Mode only against a local fake (tests/test_gd8_slack.py), never slack.com
    "SLACK_MOCK": "1", "SLACK_API_BASE": os.environ.get("SLACK_API_BASE", "http://127.0.0.1:8783/api/"),
    "SLACK_SUPERVISOR_S": os.environ.get("SLACK_SUPERVISOR_S", "2"),
    # GĐ8.5: Google Workspace / Microsoft 365 (OAuth + REST) only against the local fake tests/fake_workspace_api.py, never Google / Microsoft
    "GW_MOCK": "1", "GOOGLE_API_BASE": os.environ.get("GOOGLE_API_BASE", "http://127.0.0.1:8784/"),
    "GOOGLE_OAUTH_BASE": os.environ.get("GOOGLE_OAUTH_BASE", "http://127.0.0.1:8784/google/oauth"),
    "GRAPH_API_BASE": os.environ.get("GRAPH_API_BASE", "http://127.0.0.1:8784/graph/v1.0/"), "MS_OAUTH_BASE": os.environ.get("MS_OAUTH_BASE", "http://127.0.0.1:8784/microsoft/oauth"),
})
if not os.environ.get("AOS_ZP_NODE"):   # GĐ8.3b: Node for the Zalo-personal sidecar: Linux Node 24 unpacked in ~/tools (WSL has none in PATH); else `node` from PATH
    import glob as _glob
    _nodes = sorted(_glob.glob(os.path.expanduser("~/tools/node-v24*-linux-x64/bin/node")))
    if _nodes: os.environ["AOS_ZP_NODE"] = _nodes[-1]
if not os.environ.get("AOS_OAUTH_KEY"):   # GĐ8.5: local throwaway Fernet key for per-user OAuth tokens (the real one stays on #83)
    _okf = "/workspace/agentos-local/var/oauth_key"
    if not os.path.exists(_okf):
        from cryptography.fernet import Fernet as _Fer
        with open(os.open(_okf, os.O_WRONLY | os.O_CREAT, 0o600), "w") as _f: _f.write(_Fer.generate_key().decode())
    with open(_okf) as _f: os.environ["AOS_OAUTH_KEY"] = _f.read().strip()
os.environ.pop("OPENAI_API_KEY", None)
# REAL-GEMINI mode (approved 21/10/2026 by anh Huy, cap $2): AOS_LOCAL_GEMINI_KEY_FILE=<600 file> -> agent chat calls go to the real
# Gemini API; everything else (embeddings, web search, judge/JSON helpers, OpenAI) stays fake/blocked. Key never printed or logged.
REAL = os.environ.pop("AOS_LOCAL_GEMINI_KEY_FILE", "")
if REAL:
    with open(os.path.expanduser(REAL)) as _f:
        os.environ["GOOGLE_API_KEY"] = _f.read().strip()
    os.environ.pop("GOOGLE_GEMINI_BASE_URL", None)
    REAL_CAP = float(os.environ.get("AOS_LOCAL_GEMINI_CAP_USD", "2"))
# A8 (approved 20/10/2026 17:33 UTC+7): the Gemini key is read from its 600 file into THIS process env only, under a name
# the rest of Agent OS does not use (its chat LLM stays the local fake). Never printed or logged.
_kf = os.environ.pop("AOS_GEMINI_LIVE_KEY_FILE", "")
if _kf and "google" in os.environ.get("AOS_VOICE_REAL_PROVIDERS", ""):
    with open(os.path.expanduser(_kf)) as _f:
        os.environ["AOS_GEMINI_LIVE_KEY"] = _f.read().strip()
    print("A8: real voice provider enabled: google (key loaded into server env, masked AQ.…)", flush=True)
import admin_pw  # noqa: E402  sets AGENTOS_OWNER_PW_HASH for local user "moza"
import gateway as gw  # noqa: E402
from fake_llm import FakeAgentModel  # noqa: E402
if REAL:   # total Gemini spend (gateway_usage, all time, this local DB) is capped; the gateway skips Gemini once it is reached
    gw.CAPS["gemini"] = REAL_CAP; gw.CAPS["openai"] = 0.0
    print(f"REAL GEMINI: agent chat uses the Gemini API (key loaded, masked), total cap ${REAL_CAP:g}; OpenAI blocked", flush=True)
else:
    os.environ["AOS_FAKE_LLM"] = "1"   # GĐ11.3: gateway.prepare() (provider key checks) is skipped under the fake model
    gw._model = lambda name, temperature=None, params=None: FakeAgentModel(model_name=f"fake:{name}")
async def _no_generate(client=None, prompt="", *a, **k):
    from fake_llm import fake_judge   # GD7.6: scripted judge for openevals/alignment tests; everything else keeps the old "{}"
    return (fake_judge(prompt) or "{}", "fake", 0, 0)
gw.generate = _no_generate
import deepagent_api as A  # noqa: E402
async def _fake_embed(texts):
    out = []
    for t in texts:
        # GD7.3: bag of words (each word = a fixed pseudo-random direction, summed, unit length) so texts sharing words are close and Insights can cluster them; no words -> hash of the text
        ws = re.findall(r"\w+", t.lower()) or [t]; v = [0.0] * A.DIMS
        for w in ws:
            h = hashlib.shake_128(w.encode()).digest(A.DIMS)
            for i in range(A.DIMS): v[i] += (h[i] - 128) / 128
        n = sum(x * x for x in v) ** 0.5 or 1.0; out.append([x / n for x in v])
    return out
A.aembed = _fake_embed
async def _fake_search(query, n=5):   # GĐ3.3: deterministic results in the real web_search text format (no network)
    rows = [("LangGraph documentation", "https://docs.langchain.com/oss/python/langgraph/overview", "LangGraph is a low-level orchestration framework."),
            ("assistant-ui", "https://www.assistant-ui.com/docs", "React components for AI chat."),
            ("Deep Agents", "https://docs.langchain.com/oss/python/deepagents/overview", "Agents that plan, use sub-agents and a file system.")]
    return "\n".join(f"{i+1}. {t}\n   {u}\n   {s}" for i, (t, u, s) in enumerate(rows[:min(int(n or 5), 3)]))
if not os.environ.get("AOS_LOCAL_REAL_SEARCH"): A._web_search = _fake_search   # GD7.R: =1 keeps the real (free, no-key) DuckDuckGo search for the deep-research check
async def _no_json(prompt): return {}
A.gemini_json = _no_json
# (fixed in deepagent_api.lifespan: seeding now runs after every module's init, so a fresh DB boots with the SEED agents)
import ext as _ext  # noqa: E402
from pathlib import Path as _P  # noqa: E402
_ext.KEY_FILE = _P("/workspace/agentos-local/var/link_key")   # local throwaway key (real one stays on #83)
import sandbox2 as _sbx  # noqa: E402
# GĐ9.Q (local test box only): the dev user is not root, so chown to nobody (65534) must not fail; sandbox services are reachable on loopback (fake runtime, no docker);
# git volume import may read file:// repositories created by the tests. None of this is in production.
_chown = os.chown
def _safe_chown(p, u, g, *a, **k):
    try: _chown(p, u, g, *a, **k)
    except PermissionError: pass
os.chown = _safe_chown
import re as _re  # noqa: E402
_sbx.SVC_IP_RE = _re.compile(r"(172\.31\.250\.\d{1,3}|127\.0\.0\.1)")
import sbx_parity as _sbxp  # noqa: E402
_sbxp.TEST_ALLOW_FILE_GIT = True
import subprocess as _sp  # noqa: E402
# GĐ8.R: AOS_LOCAL_REAL_SANDBOX=1 -> chat code runs use the real sandbox (docker, gVisor runsc if registered, else runc with the same
# limits, no network) on image AOS_SBX_DATA_IMAGE (default agentos-sbx-py:20261023, sandbox/Dockerfile.py-data). Default: fake runner.
REAL_SBX = os.environ.get("AOS_LOCAL_REAL_SANDBOX") == "1"
if REAL_SBX:
    _img = os.environ.get("AOS_SBX_DATA_IMAGE") or "agentos-sbx-py:20261023"
    if _sp.run(["docker", "image", "inspect", _img], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL).returncode != 0:
        print(f"REAL SANDBOX: image {_img} missing (build sandbox/Dockerfile.py-data) -> fake runner", flush=True); REAL_SBX = False
if not REAL_SBX:
    async def _no_docker(*a, **k): return 127, "", "docker not available on the box (local spike)"
    _sbx._sh = _no_docker
import asyncio  # noqa: E402
import chat_gd5b as _g5b  # noqa: E402
async def _fake_code(code, timeout):   # GĐ5b: no Docker on the box -> deterministic stand-in for the gVisor runner (same output protocol)
    import json as _j, base64 as _b
    if "while True" in code: return 124, "", "timeout"
    png = _b.b64encode(_PNG).decode() if "plt." in code else None
    if "raise" in code: return 0, _g5b.MARK + _j.dumps({"ok": False, "stdout": "", "stderr": "Traceback ...\nValueError: lỗi thử", "images": []}), ""
    # the rest really runs (local python3 subprocess, no matplotlib: plt lines are dropped and a fixed PNG stands in for the chart)
    A.SBX_INFO.update(runtime="giả lập (local, không có Docker)")
    src = "\n".join(l for l in code.splitlines() if "plt" not in l)
    p = await asyncio.create_subprocess_exec("python3", "-I", "-c", src, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try: so, se = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError: p.kill(); return 124, "", "timeout"
    return 0, _g5b.MARK + _j.dumps({"ok": p.returncode == 0, "stdout": so.decode()[-20000:], "stderr": se.decode()[-8000:], "images": [png] if png else []}), ""
_PNG = __import__("base64").b64decode("iVBORw0KGgoAAAANSUhEUgAAAKAAAABaCAIAAACwpMoFAAABSUlEQVR42u3csQ3CQBBFQTdDZTRxPVOEISKyJQjv7XzRweiJ1QU+zputtU7bfwdgwAbYABtgA2yADTBgwIANsAE2wAbYLvZ4vj4/wFlawGVXwH1a/8FN1y8t4Gayruh4soDjyQKOJws4nizgeLKA48kCnkILuOwKuE8LePsDCvDEZAHHkwUcTxZwPFnA8WQBx5MFHE8WcDxZwPFkAU+hBVx2BdynBbz9AQV4YrKA48kCjic7GnhCskOBp7lOAR6Y7BTg4a5ZYMlmgblmgdE2gblmgdE2gR1QWWCuTWDJZoG5NoElmwXm2gSWbBaYaxNYsllgrllgtE1grllgtE1gB1QWmGsTWLJZYK5NYMlmgbk2gSWbBebaBJZsFphrFviS1ld2IsB3yQKuAf/10GE7Af/+0GH7AS9Lz3eypj50AAZsgA2wATbAdrM36sS44M1ykFwAAAAASUVORK5CYII=")
if REAL_SBX:   # runtime is detected at startup by sandbox2.init (gvisor (runsc) / runc)
    _g5b.SBX_DATA_IMAGE = _img
    print(f"REAL SANDBOX: chat code runs in docker image {_img} (no network, 512 MB)", flush=True)
else:
    _g5b.RUNNER["fn"] = _fake_code
    A.SBX_INFO.update(runtime="giả lập (local, không có Docker)")
import chat_overlay  # noqa: E402
A.app.router.routes[0:0] = chat_overlay.router().routes   # first, so no catch-all page route shadows them
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8020")), log_level="info")
