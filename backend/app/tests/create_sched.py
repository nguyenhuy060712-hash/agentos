import json, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
ids = json.load(open("team_ids.json")); evs = json.load(open("eval_ids.json"))
have = {x["name"] for x in s.get(U + "/api/agent_crons").json()}
CR = [(ids["lead"], "Phòng AI số · weekly report", "30 8 * * 1", "Weekly review: ask analytics for last week's KPIs (ads + CRM) and crm-sales for the pipeline and due follow-ups; then write the weekly report with 3 priorities. Do not launch ads or publish anything."),
      (ids["AI CRM-Sales"], "CRM · weekday follow-up sweep", "0 9 * * 1-5", "Morning sweep: list deals, find those without a scheduled follow-up and schedule one per the playbook. Report what you scheduled."),
      (ids["AI Analytics"], "Analytics · Friday KPI snapshot", "0 17 * * 5", "Compute this week's CTR, CPA, ROAS estimate and pipeline value in the sandbox; output a table + 3 insights.")]
for a, n, cr, m in CR:
    if n not in have: print(n, s.post(U + "/api/agent_crons", json={"agent_id": a, "name": n, "cron": cr, "message": m, "new_thread": True}).json())
rules = {r["name"] for r in s.get(U + "/api/obs/rules").json()}
if "Phòng AI số · online eval" not in rules:
    print(s.post(U + "/api/obs/rules", json={"project_id": int("1"), "name": "Phòng AI số · online eval", "filter": 'eq(metadata.agent_id,"%s")' % ids["lead"], "sampling_rate": 1,
        "actions": [{"type": "run_evaluator", "evaluator_id": evs[k]} for k in ("phong_lead_delegation", "phong_crm_logged_followup", "phong_analytics_sandbox")]}).text)
