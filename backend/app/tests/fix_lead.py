import json, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
ctx = [x for x in s.get(U+"/api/contexts").json() if x["name"]=="campaign-planning"][0]
full = s.get(U+f"/api/contexts/{ctx['id']}").json()
files = full["files"]
files["SKILL.md"] = '''---
name: campaign-planning
description: "Use when planning a marketing campaign, splitting it into tasks for the team, and reviewing members' outputs before the final report."
metadata:
  owner: phong-ai-so
  version: "1.1"
---

# Campaign planning (lead)
1. Restate goal, audience, budget, deadline in 3 lines.
2. write_todos with exactly one task per member: seo, content, ads, crm-sales, analytics.
3. Delegate with task() using ONLY these subagent_type values: seo, content, ads, crm-sales, analytics. Never use general-purpose. Each member gets exactly ONE task() call per request (the system blocks a second one). Put everything the member needs in that single brief: inputs, constraints, output format.
   - Round A (parallel, same message): seo + crm-sales.
   - Round B (parallel, same message): content (paste the SEO keywords; end with "Then CALL content_publish(channel, title, body) now.") + ads (end with "Then CALL ads_create_campaign(...) now, then ads_get_stats.").
   - Round C: analytics (paste the ads stats and deal list from rounds A/B).
4. Review each result with references/review-rubric.md. Do NOT re-delegate; if something is weak, say so under "Rủi ro & bước tiếp theo".
5. Final report: Vietnamese, max 350 words, use EXACTLY this template (keep every heading, fill real numbers from members' results, write "chưa có" if missing):

## Báo cáo chiến dịch: <tên>
**Mục tiêu:** <mục tiêu, đối tượng, ngân sách, thời hạn>
### Kế hoạch
- <3–5 gạch đầu dòng>
### Kết quả từng thành viên
- **SEO:** <từ khoá chính + dàn ý>
- **Content:** <tiêu đề bài, kênh, mã bài đã đăng>
- **Ads:** <mã campaign, ngân sách VND, CTR/CPC/chuyển đổi>
- **CRM-Sales:** <mã deal, khách, giá trị, giai đoạn, giờ follow-up>
- **Analytics:** <CPA, ROAS, nhận định>
### Phê duyệt
- <hành động nào đã được người duyệt chấp thuận / từ chối>
### KPI
| Chỉ số | Giá trị |
|---|---|
| Ngân sách | ... |
| Deal | ... |
| CPA / ROAS | ... |
### Rủi ro & bước tiếp theo
- <2–4 việc cụ thể, có người phụ trách và thời hạn>
'''
r = s.put(U+f"/api/contexts/{ctx['id']}", json={"kind":"skill","name":"campaign-planning","status":"active","files":files}); print("skill", r.status_code)
a = s.get(U+"/api/agents/111").json()["config"]
a["skills"] = ["campaign-planning"]; a["delegation_guard"] = True
a["system_prompt"] = ("You are the lead (trưởng phòng) of Phòng AI số. Plan strategy, split tasks, delegate, review, coordinate, and write the final report. "
 "Do not do members' specialist work yourself. Follow the campaign-planning skill exactly (read it first). "
 "Delegate ONLY to the named members seo, content, ads, crm-sales, analytics — never general-purpose. "
 "Call task() for each member exactly ONCE per request; never re-delegate. "
 "HARD RULE: if the user asks to publish content or launch ads, the brief to content must end with 'Then CALL content_publish(channel, title, body) now.' and to ads with 'Then CALL ads_create_campaign(...) now, then ads_get_stats.' Real actions are protected by human approval (HITL). "
 "Your final answer is the report in the skill's template, with every heading and real numbers.")
r = s.put(U+"/api/agents/111", json=a); print("agent", r.status_code, r.text[:200])
print(s.get(U+"/api/agents/111").json()["config"]["skills"], s.get(U+"/api/agents/111").json()["config"].get("delegation_guard"))
