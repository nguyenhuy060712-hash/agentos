import json, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
def code(key, names, need=1):
    return f"""def evaluate(run, example):
    act = [t.get('name') for t in run.get('trajectory') or []]
    want = {names!r}
    hit = [n for n in want if n in act]
    return {{'score': round(len(hit) / len(want), 3), 'comment': 'used: ' + ', '.join(hit) + ' | trajectory: ' + ' > '.join(act[:25])}}
"""
EV = [
 ("phong_seo_used_search", "code", {"key": "seo", "code": code("seo", ["web_search"])}),
 ("phong_content_published", "code", {"key": "content", "code": code("content", ["content_publish"])}),
 ("phong_ads_launched", "code", {"key": "ads", "code": code("ads", ["ads_create_campaign", "ads_get_stats"])}),
 ("phong_crm_logged_followup", "code", {"key": "crm", "code": code("crm", ["crm_log_deal", "crm_schedule_followup"])}),
 ("phong_analytics_sandbox", "code", {"key": "analytics", "code": code("analytics", ["chay_code_python"])}),
 ("phong_lead_delegation", "code", {"key": "delegation", "code": """def evaluate(run, example):
    act = [t.get('name') for t in run.get('trajectory') or []]
    n = act.count('task')
    return {'score': min(1.0, n / 5), 'comment': f'{n} task() delegations; write_todos={act.count("write_todos")}'}
"""}),
 ("phong_lead_report_quality", "llm", {"key": "report_quality", "score_type": "numeric", "min": 1, "max": 5, "prompt":
  "You grade the final report of a marketing team lead (Vietnamese).\nRequest: {{input}}\nReport: {{output}}\n5 = clear plan, results per member (SEO, content, ads, CRM-sales, analytics), concrete numbers (budget, KPI, deal ids, follow-up times), approvals mentioned, next steps; 3 = partly; 1 = vague/off-brief."}),
]
have = {e["name"]: e["id"] for e in s.get(U + "/api/evaluators").json()}
ids = {}
for n, k, cfg in EV:
    r = s.put(U + f"/api/evaluators/{have[n]}", json={"name": n, "kind": k, "config": cfg}) if n in have else s.post(U + "/api/evaluators", json={"name": n, "kind": k, "config": cfg})
    r.raise_for_status(); ids[n] = have.get(n) or r.json()["id"]
print(ids); json.dump(ids, open("/workspace/adk_ha_build/tests/eval_ids.json", "w"))
