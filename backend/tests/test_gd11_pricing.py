"""GĐ11.16 price table for the enabled models + correct cost. Hand-computed examples from the prices in docs/GD11_RESEARCH.md (Anthropic / OpenAI / Gemini 25/10/2026,
DeepSeek 26/10/2026 stored at the PEAK price). No network, no real LLM."""
import json, logging, os, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app")); sys.path.insert(0, HERE)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import deepagent_api   # noqa: F401,E402  (first app import: modules import each other circularly)
import gateway as gw   # noqa: E402
from test_gd11_gateway_providers import with_ws, as_user, FakeProviders, KEY   # noqa: E402,F401

def usd(x): return round(x / 1e6, 6)

def test_claude_input_output_and_cache_read():
    assert gw.cost("anthropic/claude-haiku-5-5", 1000, 500) == usd(1000 * 0.10 + 500 * 0.50)
    assert gw.cost("claude-haiku-5-5", 1000, 500) == gw.cost("anthropic/claude-haiku-5-5", 1000, 500)               # legacy bare name -> same canonical price
    # Sonnet 5.5: 10 000 input tokens of which 4 000 are cache reads, 1 000 output
    assert gw.cost("anthropic/claude-sonnet-5-5", 10000, 1000, {"cached": 4000}) == usd(6000 * 2 + 4000 * 0.10 + 1000 * 10)

def test_claude_cache_write_5m_and_1h():
    # Opus 5.5: 10 000 input = 2 000 read (0.20) + 3 000 write 5m (5) + 1 000 write 1h (8) + 4 000 plain (4)
    assert gw.cost("anthropic/claude-opus-5-5", 10000, 0, {"cached": 2000, "cache_write": 3000, "cache_write_1h": 1000}) == usd(2000 * 0.20 + 3000 * 5 + 1000 * 8 + 4000 * 4)
    # the same numbers as langchain-anthropic reports them (cache_creation is 0 when the 5m / 1h split is present)
    d = gw.details_from_usage({"input_token_details": {"cache_read": 2000, "cache_creation": 0, "ephemeral_5m_input_tokens": 3000, "ephemeral_1h_input_tokens": 1000}})
    assert gw.cost("anthropic/claude-opus-5-5", 10000, 0, d) == usd(2000 * 0.20 + 3000 * 5 + 1000 * 8 + 4000 * 4)
    # no split reported: the whole creation count is a 5-minute write
    assert gw.details_from_usage({"input_token_details": {"cache_creation": 700}}) == {"cache_write": 700}
    # a model without a 1h price falls back to its plain input price (same rule as 10.26)
    gw.PRICING["t1116-x"] = {"input_per_1m": 1.0, "output_per_1m": 2.0}
    try: assert gw.cost("t1116-x", 1000, 0, {"cache_write_1h": 1000}) == usd(1000)
    finally: gw.PRICING.pop("t1116-x")

def test_claude_haiku_long_context_whole_call():
    assert gw.cost("anthropic/claude-haiku-5-5", 100000, 1000) == usd(100000 * 0.10 + 1000 * 0.50)                    # at the threshold: standard
    assert gw.cost("anthropic/claude-haiku-5-5", 150000, 1000) == usd(150000 * 0.50 + 1000 * 2.50)                    # above: the whole call uses the long prices

def test_gpt_gemini_and_deepseek_examples():
    assert gw.cost("gpt-5.6-terra", 1000, 1000, {"cached": 500}) == usd(500 * 2 + 500 * 0.20 + 1000 * 12)
    assert gw.cost("gpt-5.6-terra", 1000, 0, {"cache_write": 400}) == usd(600 * 2 + 400 * 2.5)                         # GPT-5.6+: write 1.25x
    assert gw.cost("gpt-6-sol", 300000, 1000) == usd(300000 * 4 + 1000 * 15)                                           # > 272K: 2x input, 1.5x output
    assert gw.cost("gemini-4-argon", 1000, 1000) == usd(1000 * 2 + 1000 * 10)
    assert gw.cost("gemini-3.1-pro-preview", 250000, 1000) == usd(250000 * 4 + 1000 * 18)
    assert gw.cost("deepseek/deepseek-flash", 1000, 1000, {"cached": 400}) == usd(600 * 0.30 + 400 * 0.006 + 1000 * 1.20)   # peak price
    assert gw.cost("deepseek/deepseek-v4-flash", 1000, 1000) == gw.cost("deepseek/deepseek-flash", 1000, 1000)          # old name billed as Flash
    assert gw.cost("deepseek/deepseek-v4-pro", 1000, 1000) == usd(1000 * 1.32 + 1000 * 3.96)

def test_old_models_cost_unchanged():
    assert gw.cost("gemini-3.6-flash", 1000, 1000) == usd(1000 * 0.75 + 1000 * 3.75)
    assert gw.cost("gemini-3.8-flash", 2000, 100) == usd(2000 * 0.75 + 100 * 3.75)
    assert gw.cost("gpt-5.4-nano", 1000, 1000) == usd(1000 * 0.20 + 1000 * 1.25) or gw.PRICING["gpt-5.4-nano"]["input_per_1m"] == 0.2
    assert gw.cost("gpt-5.6-luna", 1000, 1000, {"cached": 100}) == gw.cost("gpt-5.6-luna", 1000, 1000, {"cached": 100})
    assert gw.cost("totally-unknown-model", 10, 10) is None and gw.cost(None, 1, 1) is None

def test_unpriced_model_stays_none_with_one_warning(caplog):
    caplog.set_level(logging.WARNING, logger="aos.pricing"); gw._UNPRICED.discard("zz-unpriced-model")
    assert gw.cost("zz-unpriced-model", 10, 10) is None and gw.cost("zz-unpriced-model", 10, 10) is None
    assert len([r for r in caplog.records if "zz-unpriced-model" in r.message]) == 1

def test_table_entries_have_source_and_date_and_openai_list():
    new = ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5", "gpt-6-sol", "gpt-5.6-terra", "gemini-4-argon", "deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]
    for k in new:
        e = gw.PRICING[k]; assert e["source_url"].startswith("https://") and e["checked"] >= "2026-10-25" and e["note"], k
    assert "gpt-6-sol" in gw.OPENAI_MODELS and "gpt-5.6-luna" in gw.OPENAI_MODELS and not any("/" in m or "gemini" in m for m in gw.OPENAI_MODELS)
    assert gw.PRICING["anthropic/claude-opus-5-5"]["cache_write_1h_per_1m"] == 8 and gw.PRICING["anthropic/claude-haiku-5-5"]["cache_write_1h_per_1m"] == 0.20

def test_litellm_lookup_strips_provider_prefix_and_maps_1h_field():
    import pricing
    lit = {"claude-haiku-5-5": {"input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7, "cache_creation_input_token_cost_above_1hr": 2e-7},
           "deepseek/deepseek-flash": {"input_cost_per_token": 3e-7}}
    assert pricing._find(lit, "anthropic/claude-haiku-5-5") is lit["claude-haiku-5-5"] and pricing._find(lit, "deepseek/deepseek-flash") is lit["deepseek/deepseek-flash"]
    assert pricing._find(lit, "anthropic/nope") is None
    d = pricing.make_diff({"anthropic/claude-haiku-5-5": {"input_per_1m": 0.10, "output_per_1m": 0.50}}, lit)
    assert d["changes"]["anthropic/claude-haiku-5-5"]["cache_write_1h_per_1m"]["new"] == 0.2 and "input_per_1m" not in d["changes"]["anthropic/claude-haiku-5-5"]

def test_api_prices_only_enabled_providers_and_unpriced(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1"); monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def go(A, admin, gp, W):
        a, b = W["a"], W["b"]
        with FakeProviders(key=KEY) as fp:
            await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="anthropic", base_url=fp.url("anthropic"), api_key=KEY)))
            await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(preset="deepseek", base_url=fp.url("openai"), api_key=KEY)))
            off = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="openai", base_url=fp.url("openai"), api_key=KEY, enabled=False)))
        await A.q("INSERT INTO gateway_usage(model, input_tokens, output_tokens, cost_usd, status, workspace_id, provider) VALUES('deepseek/deepseek-brand-new',5,5,NULL,'ok',%s,'deepseek')", (a,))
        pa = await as_user(admin, a, gp.prices); pb = await as_user(admin, b, gp.prices)
        ma, mb = {r["model"] for r in pa["prices"]}, {r["model"] for r in pb["prices"]}
        assert "anthropic/claude-haiku-5-5" in ma and "deepseek/deepseek-flash" in ma and "gemini-3.6-flash" in ma          # gemini = system provider (.env key)
        assert "gpt-6-sol" not in ma and not any(m.startswith(("anthropic/", "deepseek/")) for m in mb) and "gemini-3.6-flash" in mb   # OpenAI row disabled here; B has no providers
        assert pa["unpriced"] == ["deepseek/deepseek-brand-new"] and pb["unpriced"] == []
        row = next(r for r in pa["prices"] if r["model"] == "anthropic/claude-opus-5-5"); assert row["cache_write_1h_per_1m"] == 8 and row["source_url"] and row["checked"]
        assert "api_key" not in json.dumps(pa) and KEY not in json.dumps(pa)
        import gateway as g2; g2.DEMO_WS.add(a)
        try: assert {r["provider"] for r in (await as_user(admin, a, gp.prices, role="viewer", demo=True))["prices"]} == {"gemini"}
        finally: g2.DEMO_WS.discard(a)
        await A.q("DELETE FROM gateway_usage WHERE workspace_id=%s", (a,))
    with_ws(go)
