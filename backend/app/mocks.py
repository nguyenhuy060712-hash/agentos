"""Phòng AI số: mock CRM / Ads / Publish (Postgres) + hooks middleware (official AgentMiddleware hooks: wrap_tool_call, after_model).
Mock thay cho API thật cho tới khi có client ID / tài khoản quảng cáo thật. Không sửa code thư viện LangChain/Deep Agents."""
import json, time, datetime as dt
from langchain_core.tools import tool as lc_tool
from langchain.agents.middleware import AgentMiddleware

SQL = """
CREATE TABLE IF NOT EXISTS crm_deals(id SERIAL PRIMARY KEY, workspace_id INT, customer TEXT NOT NULL, contact TEXT, product TEXT, value_vnd BIGINT DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'lead', note TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS crm_followups(id SERIAL PRIMARY KEY, deal_id INT REFERENCES crm_deals(id) ON DELETE CASCADE, due TIMESTAMPTZ NOT NULL, action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled', cron_id INT, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ads_campaigns(id SERIAL PRIMARY KEY, workspace_id INT, name TEXT NOT NULL, channel TEXT, budget_vnd BIGINT, audience TEXT, creative TEXT,
  status TEXT NOT NULL DEFAULT 'active', impressions INT DEFAULT 0, clicks INT DEFAULT 0, conversions INT DEFAULT 0, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS published_posts(id SERIAL PRIMARY KEY, workspace_id INT, channel TEXT, title TEXT, body TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS hook_log(id BIGSERIAL PRIMARY KEY, agent_id INT, kind TEXT, name TEXT, ms INT, ok BOOLEAN, detail TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now());
"""
ADS_BUDGET_MAX = 20_000_000   # guardrail: một chiến dịch mock không quá 20 triệu VND
_ready = False

async def init():
    """DDL for the mock tables, run once at startup by the owner role (lifespan, before RLS). Errors are raised, not swallowed."""
    import deepagent_api as A
    global _ready
    for st in [x for x in SQL.split(";") if x.strip()]: await A.q(st)
    _ready = True

async def _q(sql, args=None, one=False):
    import deepagent_api as A
    return await A.q(sql, args, one=one)

async def _ws(agent_id):
    import deepagent_api as A
    r = await A.q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True); return r and r["workspace_id"]

def tools(agent_id):
    @lc_tool
    async def crm_log_deal(customer: str, product: str, value_vnd: int = 0, contact: str = "", stage: str = "lead", note: str = "") -> str:
        """Log a new deal into the CRM (mock CRM in Postgres). stage: lead|qualified|proposal|won|lost. Returns the deal id."""
        r = await _q("INSERT INTO crm_deals(workspace_id,customer,contact,product,value_vnd,stage,note) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (await _ws(agent_id), customer, contact, product, int(value_vnd or 0), stage, note), one=True)
        return f"Deal #{r['id']} logged ({customer}, {product}, {value_vnd:,} VND, stage={stage})."
    @lc_tool
    async def crm_update_deal(deal_id: int, stage: str, note: str = "") -> str:
        """Update a CRM deal stage (lead|qualified|proposal|won|lost) and append a note."""
        await _q("UPDATE crm_deals SET stage=%s, note=coalesce(note,'')||%s, updated=now() WHERE id=%s", (stage, ("\n" + note) if note else "", deal_id))
        return f"Deal #{deal_id} → {stage}."
    @lc_tool
    async def crm_list_deals(stage: str = "") -> str:
        """List CRM deals (optionally by stage) with their scheduled follow-ups."""
        rs = await _q("SELECT d.id,d.customer,d.product,d.value_vnd,d.stage,(SELECT string_agg(to_char(f.due,'YYYY-MM-DD HH24:MI')||' '||f.action,'; ') FROM crm_followups f WHERE f.deal_id=d.id AND f.status='scheduled') fu "
                      "FROM crm_deals d WHERE workspace_id IS NOT DISTINCT FROM %s AND (%s='' OR stage=%s) ORDER BY id DESC LIMIT 30", (await _ws(agent_id), stage, stage))
        return "\n".join(f"#{r['id']} {r['customer']} · {r['product']} · {r['value_vnd']:,} VND · {r['stage']} · follow-up: {r['fu'] or '-'}" for r in rs) or "No deals."
    @lc_tool
    async def crm_schedule_followup(deal_id: int, due_iso: str, action: str) -> str:
        """Schedule a follow-up for a deal at due_iso (e.g. 2026-10-22T09:00, Asia/Bangkok). Creates a cron job that wakes the CRM-sales assistant at that time."""
        due = dt.datetime.fromisoformat(due_iso)
        ws = await _ws(agent_id)
        crm = await _q("SELECT id FROM agents WHERE workspace_id IS NOT DISTINCT FROM %s AND name='AI CRM-Sales' ORDER BY id LIMIT 1", (ws,), one=True)
        target = crm["id"] if crm else agent_id
        cron = f"{due.minute} {due.hour} {due.day} {due.month} *"
        c = await _q("INSERT INTO agent_crons(agent_id,name,cron,message,new_thread) VALUES(%s,%s,%s,%s,true) RETURNING id",
                     (target, f"follow-up deal #{deal_id}", cron, f"Follow-up due for deal #{deal_id}: {action}. Check crm_list_deals, do the follow-up, update the deal."), one=True)
        f = await _q("INSERT INTO crm_followups(deal_id,due,action,cron_id) VALUES(%s,%s,%s,%s) RETURNING id", (deal_id, due, action, c["id"]), one=True)
        return f"Follow-up #{f['id']} for deal #{deal_id} at {due:%Y-%m-%d %H:%M} (cron '{cron}', job #{c['id']})."
    @lc_tool
    async def ads_create_campaign(name: str, channel: str, budget_vnd: int, audience: str, creative: str) -> str:
        """Create and launch an ad campaign on the (mock) ads platform. Spends money → always requires human approval."""
        if int(budget_vnd) > ADS_BUDGET_MAX: return f"Rejected by guardrail: budget {budget_vnd:,} VND > max {ADS_BUDGET_MAX:,} VND."
        r = await _q("INSERT INTO ads_campaigns(workspace_id,name,channel,budget_vnd,audience,creative) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                     (await _ws(agent_id), name, channel, int(budget_vnd), audience, creative[:2000]), one=True)
        return f"Campaign #{r['id']} '{name}' launched on {channel}, budget {int(budget_vnd):,} VND."
    @lc_tool
    async def ads_get_stats(campaign_id: int = 0) -> str:
        """Get (mock, deterministic) performance stats for ad campaigns: impressions, clicks, CTR, conversions, CPA. campaign_id=0 → all."""
        rs = await _q("SELECT * FROM ads_campaigns WHERE workspace_id IS NOT DISTINCT FROM %s AND (%s=0 OR id=%s) ORDER BY id", (await _ws(agent_id), campaign_id, campaign_id))
        out = []
        for r in rs:
            imp = r["budget_vnd"] // 40; clk = imp * (17 + r["id"] % 9) // 1000; conv = max(1, clk * (3 + r["id"] % 4) // 100)
            out.append({"id": r["id"], "name": r["name"], "channel": r["channel"], "budget_vnd": r["budget_vnd"], "impressions": imp, "clicks": clk,
                        "ctr_pct": round(100 * clk / max(imp, 1), 2), "conversions": conv, "cpa_vnd": r["budget_vnd"] // conv})
        return json.dumps(out, ensure_ascii=False) if out else "No campaigns."
    @lc_tool
    async def content_publish(channel: str, title: str, body: str) -> str:
        """Publish a post to a channel (mock: website blog / Facebook page / Zalo OA). Public action → requires human approval."""
        r = await _q("INSERT INTO published_posts(workspace_id,channel,title,body) VALUES(%s,%s,%s,%s) RETURNING id", (await _ws(agent_id), channel, title, body[:20000]), one=True)
        return f"Post #{r['id']} published on {channel}: {title}"
    return {f"local:{t.name}": t for t in (crm_log_deal, crm_update_deal, crm_list_deals, crm_schedule_followup, ads_create_campaign, ads_get_stats, content_publish)}

class AuditHooks(AgentMiddleware):
    """Hooks qua extension point chính thức của LangChain (wrap_tool_call / after_model):
    ghi nhật ký mọi tool call (thời gian, ok/lỗi), guardrail chặn lộ secret trong tham số tool, đếm lượt model."""
    def __init__(self, agent_id):
        super().__init__(); self.agent_id = agent_id
    async def awrap_tool_call(self, request, handler):
        name = request.tool_call.get("name"); args = json.dumps(request.tool_call.get("args", {}), ensure_ascii=False)
        import admin as _adm
        if name in _adm.DEMO_TOOLS_DENY and _adm.is_demo():   # demo account: write/spend/publish/code tools never run
            from langchain_core.messages import ToolMessage
            await _log(self.agent_id, "guardrail", name, 0, False, "blocked: demo account")
            return ToolMessage(content="Tài khoản demo: thao tác này (ghi CRM / tạo quảng cáo / đăng bài / chạy code) bị khoá. Hãy mô tả kết quả dự kiến thay vì thực hiện.",
                               tool_call_id=request.tool_call["id"], name=name)
        if any(k in args for k in ("sk-", "OPENAI_API_KEY", "BEGIN PRIVATE KEY")):
            from langchain_core.messages import ToolMessage
            await _log(self.agent_id, "guardrail", name, 0, False, "blocked: secret-like value in tool args")
            return ToolMessage(content="Blocked by guardrail: tool arguments look like a secret.", tool_call_id=request.tool_call["id"], name=name)
        t = time.perf_counter(); ok = True
        try:
            return await __import__("durable").guard(request, handler)   # GĐ13.30: write tools of a queue run act once per (AIMessage, tool_call_id) even if the run is retried; else = handler(request)
        except Exception as e:
            ok = False
            if type(e).__name__ == "GraphInterrupt": ok, name = True, f"{name} (HITL pause)"
            raise
        finally:
            await _log(self.agent_id, "tool", name, int((time.perf_counter() - t) * 1000), ok, args[:300])
    async def aafter_model(self, state, runtime):
        await _log(self.agent_id, "model", "after_model", 0, True, f"messages={len(state.get('messages', []))}")
        return None

async def _log(aid, kind, name, ms, ok, detail):
    try: await _q("INSERT INTO hook_log(agent_id,kind,name,ms,ok,detail) VALUES(%s,%s,%s,%s,%s,%s)", (aid, kind, name, ms, ok, detail))
    except Exception: pass


class DelegationGuard(AgentMiddleware):
    """Official wrap_tool_call hook for coordinator agents: blocks delegation to the
    auto-added general-purpose subagent and a second delegation to the same member
    within one user turn (the lead must reuse the first result)."""
    def __init__(self, agent_id, allowed):
        super().__init__(); self.agent_id = agent_id; self.allowed = set(allowed)
    async def awrap_tool_call(self, request, handler):
        tc = request.tool_call
        if tc.get("name") != "task":
            return await handler(request)
        from langchain_core.messages import ToolMessage, HumanMessage, AIMessage
        st = (tc.get("args") or {}).get("subagent_type", "")
        def deny(msg):
            return ToolMessage(content=msg, tool_call_id=tc["id"], name="task")
        if st not in self.allowed:
            await _log(self.agent_id, "guardrail", "task", 0, False, f"blocked subagent_type={st}")
            return deny(f"Blocked: delegate only to a named member: {sorted(self.allowed)}. Pick the member whose role matches; do not use {st!r}.")
        msgs = list((request.state or {}).get("messages", []))
        last_h = max([i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)] or [0])
        for m in msgs[last_h:]:
            if isinstance(m, AIMessage):
                for c in (m.tool_calls or []):
                    if c.get("name") == "task" and c.get("id") != tc["id"] and (c.get("args") or {}).get("subagent_type") == st:
                        await _log(self.agent_id, "guardrail", "task", 0, False, f"duplicate delegation to {st}")
                        return deny(f"Blocked: {st} was already delegated in this turn. Reuse its result; do not delegate again.")
                if tc["id"] in [c.get("id") for c in (m.tool_calls or [])]:
                    break
        return await handler(request)
