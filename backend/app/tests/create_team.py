"""Create 'Phòng AI số' in Agent OS via public API (idempotent-ish: skips existing names). Prints ids."""
import json, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
def sk(name, desc, body, extra=None):
    f = {"SKILL.md": f"---\nname: {name}\ndescription: {json.dumps(desc)}\nmetadata:\n  owner: phong-ai-so\n  version: \"1.0\"\n---\n\n{body}\n"}
    f.update(extra or {}); return {"kind": "skill", "name": name, "description": desc, "status": "active", "visibility": "private", "files": f}
SKILLS = [
 sk("campaign-planning", "Use when planning a marketing campaign, splitting it into tasks for the team, and reviewing members' outputs before the final report.",
 """# Campaign planning (lead)
1. Restate goal, audience, budget, deadline in 3 lines.
2. Write a to-do plan (write_todos) with one task per member: seo, content, ads, crm-sales, analytics.
3. Delegate with task(): give each member ONE clear brief with inputs, constraints and the output format. Order: round A in parallel = seo + crm-sales; round B = content (paste the SEO brief into its task) + ads; round C = analytics (paste ads stats and deal list). When the user asks to publish/launch, tell the member explicitly to CALL content_publish / ads_create_campaign — the system pauses for human approval automatically.
4. Review each result with the rubric in references/review-rubric.md. If a result scores < 3, send ONE revision brief to that member.
5. Final report: Plan · Results per member · Approvals needed/done · KPIs · Next steps. Max 350 words.""",
 {"references/review-rubric.md": "# Review rubric (1-5)\n- Matches the brief and audience\n- Concrete numbers (budget, KPI, dates)\n- Brand rules from AGENTS.md respected\n- Actionable next step\nScore 5 = all; 3 = two missing; 1 = off-brief."}),
 sk("seo-keyword-brief", "Use when asked for keyword research, search intent, or an SEO brief (title, meta description, outline) for a page or blog post.",
 """# SEO brief
- Use web_search for 1-2 queries max (cost control). Pick 1 primary + 4 secondary Vietnamese keywords with intent (informational/commercial).
- Output: Primary keyword · Secondary keywords · Title (≤60 chars) · Meta description (≤155 chars) · H2 outline (4-6 items) · Internal link ideas.
- Never invent search volumes; say "ước tính" if unsure."""),
 sk("content-writing", "Use when writing blog posts, social posts, captions or ad copy in the brand voice, and when publishing content to a channel.",
 """# Content writing
- Voice: friendly, concrete, Vietnamese, no exaggerated claims (see AGENTS.md).
- Blog: ≤400 words, use the SEO brief's title and H2s if given. Social: ≤80 words + 1 CTA + max 3 hashtags.
- Publishing: call content_publish only when the lead asked to publish. It needs human approval; if rejected, report the reviewer's reason and stop.
- Return the final text plus a 1-line summary."""),
 sk("ads-campaign-launch", "Use when proposing, launching or reporting paid ad campaigns (Facebook/Google/Zalo ads), budgets, audiences and creatives.",
 """# Ads launch
1. Propose: channel, audience, daily/total budget (VND), creative (headline + text), KPI target (CTR, CPA).
2. Budget rule: one campaign ≤ 20,000,000 VND (guardrail also enforces it).
3. Launch with ads_create_campaign → this pauses for human approval (HITL). Never claim it launched unless the tool returned 'launched'.
4. After launch, read ads_get_stats and report impressions, clicks, CTR, conversions, CPA."""),
 sk("crm-sales-playbook", "Use when handling leads or customers: qualifying, selling, logging deals into the CRM, updating deal stages and scheduling follow-ups.",
 """# CRM & sales playbook
- Every new lead/customer conversation → crm_log_deal (customer, product, value_vnd, stage).
- Stages: lead → qualified → proposal → won/lost. Update with crm_update_deal and a short note.
- Always schedule the next follow-up with crm_schedule_followup (ISO time, Asia/Bangkok): lead +1 day, proposal +2 days, won +7 days (thank-you/upsell).
- Sales message: greet, 1 benefit tied to the customer's need, clear offer, 1 question. ≤70 words.
- Report: deals logged (ids), stages, follow-ups scheduled (time + action)."""),
 sk("campaign-analytics", "Use when calculating marketing KPIs (CTR, CPA, ROAS, conversion rate, pipeline value) or analysing campaign/CRM data with Python.",
 """# Campaign analytics
- Get data with ads_get_stats and crm_list_deals, then compute in the Python sandbox (chay_code_python) — never do arithmetic in your head.
- KPIs: CTR = clicks/impressions; CPA = spend/conversions; ROAS = pipeline_or_revenue/spend; win rate = won/(won+lost).
- Output a small table + 3 insights + 1 recommendation. Round to 2 decimals.""",
 {"references/kpi.md": "ROAS ≥ 3 is good for coffee B2C; CTR ≥ 1.5% good on Facebook; CPA target ≤ 80,000 VND."}),
]
MEM = {"kind": "agent", "name": "phong-ai-so", "description": "Brand & team rules for Phòng AI số", "status": "active",
 "files": {"AGENTS.md": """# Phòng AI số — fixed memory
Company: Cà phê Moza (fictional demo brand) — specialty coffee beans & drip bags, sells online (website, Facebook, Zalo) to consumers and offices in HCMC/Hanoi.
Language: answer in Vietnamese; keep standard English marketing terms (SEO, CTR, CPA, ROAS, CRM).
Brand voice: warm, honest, concrete; no health claims, no "số 1", no fake reviews.
Money: any ad spend or public post needs human approval. Total campaign budget unless told otherwise: 30,000,000 VND/month.
Prices: drip bag box (10 gói) 159,000 VND; 500g beans 289,000 VND; office plan 2,500,000 VND/month.
Timezone: Asia/Bangkok (UTC+7)."""}}
def ctx(b):
    ex = [x for x in s.get(U + "/api/contexts").json() if x["name"] == b["name"]]
    r = s.put(U + f"/api/contexts/{ex[0]['id']}", json=b) if ex else s.post(U + "/api/contexts", json=b)
    r.raise_for_status(); return r.json()
for b in SKILLS + [MEM]: print("ctx", b["name"], ctx(b))
M = "gpt-5.4-nano"
base = {"model": M, "fallback_models": [], "temperature": 0.3, "rpm_limit": 30, "budget_usd_day": 0.2, "memory": ["phong-ai-so"], "planning": False, "filesystem": False}
MEMBERS = {
 "AI SEO": dict(description="SEO specialist: keyword research and SEO briefs.", system_prompt="You are the SEO specialist of Phòng AI số. Always run exactly one web_search first, then follow your skill. Be concise.", tools=["local:web_search"], skills=["seo-keyword-brief"]),
 "AI Content": dict(description="Content writer: blog/social/ad copy in brand voice; publishes posts (needs approval).", system_prompt="You are the content writer of Phòng AI số. Follow your skill. If the brief says publish, you MUST call content_publish with the final text (the system pauses for human approval automatically; never ask for approval in text).", tools=["local:content_publish"], skills=["content-writing"], interrupt_on=["local:content_publish"]),
 "AI Ads": dict(description="Paid ads specialist: proposes and launches campaigns (needs approval), reports stats.", system_prompt="You are the paid-ads specialist of Phòng AI số. Follow your skill. If the brief says launch, you MUST call ads_create_campaign (the system pauses for human approval automatically; never ask in text), then call ads_get_stats.", tools=["local:ads_create_campaign", "local:ads_get_stats"], skills=["ads-campaign-launch"], interrupt_on=["local:ads_create_campaign"]),
 "AI CRM-Sales": dict(description="Sales + CRM: sells to leads, logs deals into CRM, updates stages, schedules and performs follow-ups itself.", system_prompt="You are the sales & CRM rep of Phòng AI số. Follow your skill. Always log deals and schedule follow-ups.", tools=["local:crm_log_deal", "local:crm_update_deal", "local:crm_list_deals", "local:crm_schedule_followup"], skills=["crm-sales-playbook"]),
 "AI Analytics": dict(description="Data analyst: computes KPIs from ads + CRM data in the Python sandbox.", system_prompt="You are the data analyst of Phòng AI số. Follow your skill; compute with chay_code_python.", tools=["local:ads_get_stats", "local:crm_list_deals", "local:chay_code_python"], skills=["campaign-analytics"]),
}
ags = {a["name"]: a["id"] for a in s.get(U + "/api/agents").json()}
def upsert(name, cfg):
    body = {**base, "name": name, **cfg}
    r = s.put(U + f"/api/agents/{ags[name]}", json=body) if name in ags else s.post(U + "/api/agents", json=body)
    if r.status_code != 200: raise SystemExit(f"{name}: {r.text}")
    j = r.json(); ags[name] = j.get("id", ags.get(name)); return ags[name]
ids = {n: upsert(n, c) for n, c in MEMBERS.items()}
subs = [{"name": sub, "agent_ref": ids[n], "description": MEMBERS[n]["description"]} for sub, n in
        [("seo", "AI SEO"), ("content", "AI Content"), ("ads", "AI Ads"), ("crm-sales", "AI CRM-Sales"), ("analytics", "AI Analytics")]]
lead = upsert("AI Lead - Trưởng phòng AI số", dict(description="Lead/coordinator: strategy, task split, review, coordination of Phòng AI số.",
    system_prompt="You are the lead (trưởng phòng) of Phòng AI số. Plan strategy, split tasks, delegate to your sub-agents with task(), review their results with your rubric, coordinate, and write the final report. Do not do members' specialist work yourself. Delegate independent tasks in parallel. Delegate to each member at most ONCE per request (re-delegate only for a revision). HARD RULE: if the user asks to publish content or launch ads, your task() brief to content must end with 'Then CALL content_publish(channel, title, body) now.' and to ads with 'Then CALL ads_create_campaign(...) now, then ads_get_stats.' Never write 'no need to actually create' — real actions are protected by human approval (HITL).",
    planning=True, filesystem=True, skills=["campaign-planning"], subagents=subs, budget_usd_day=0.4, interrupt_on=["local:ads_create_campaign", "local:content_publish"]))
ids["lead"] = lead
print(json.dumps(ids, ensure_ascii=False))
json.dump(ids, open("/workspace/adk_ha_build/tests/team_ids.json", "w"), ensure_ascii=False)
