"""GĐ11.29 run the 11.30 fake OAuth + MCP server by hand (for the browser flow of app/tests/shots_gd11_connect.py): `python tests/fakes/serve_fake_mcp_oauth.py [mode] [port-file]`.
Prints one JSON line {"base","mcp_url","mode"} and serves until killed (by its PID). $0, 127.0.0.1 only. The signed-in 'person' of the fake sign-in page is set with FAKE_SUB (default user-1)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mcp_oauth import FakeOAuthMcp  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "none"
f = FakeOAuthMcp(mode).start(); f.sub = os.environ.get("FAKE_SUB", "user-1"); f.token_ttl = int(os.environ.get("FAKE_TTL", "3600"))
line = json.dumps({"base": f.base, "mcp_url": f.mcp_url, "mode": mode}); print(line, flush=True)
if len(sys.argv) > 2: open(sys.argv[2], "w").write(line)
while True: time.sleep(3600)
