"""GĐ8.Z review fix (24/10/2026): a channel run (Telegram / Zalo / Slack) acts as the binding owner for RLS and costs, but an external
chatter must never reach the owner's connected accounts (Google / Microsoft OAuth tokens), personal instructions or personal memories.
Pure in-process tests (no DB, no network)."""
import asyncio, os, sys
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "test-not-a-key")   # import-time client only; nothing is called

def test_get_token_refused_on_channel_runs():
    import deepagent_api, oauth_tools   # noqa: F401  (deepagent_api first: it imports oauth_tools.router)
    with pytest.raises(HTTPException) as e:
        asyncio.run(oauth_tools.get_token("google", {"id": 1, "ws": 1, "auth": "channel", "chat": "b1:42"}))
    assert e.value.status_code == 403

def test_memory_namespace_per_external_chatter():
    import admin, deepagent_api as A
    t = admin.USER.set({"id": 7, "ws": 1, "role": "admin", "auth": "channel", "chat": "b3:alice"})
    try: assert A._mem_ns(5) == ("agent", "5", "ch", "b3:alice", "memories")
    finally: admin.USER.reset(t)
    t = admin.USER.set({"id": 7, "ws": 1, "role": "admin", "auth": "run"})
    try: assert A._mem_ns(5) == ("agent", "5", "u7", "memories")   # a normal chat run keeps the per-user namespace
    finally: admin.USER.reset(t)
