"""Skills/memory per deepagents 0.7.28 (SKILL.md frontmatter, publish snapshots, pin on save, per-subagent skills, AGENTS.md memory).
Self-cleaning. Run: python3 skills_test.py [live]  (live = 1 cheap gpt-5.4-nano run)"""
import sys, uuid, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
R = {}; tag = uuid.uuid4().hex[:5]; made_ctx, made_ag = [], []
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:200], flush=True)
nm = f"t-skill-{tag}"
try:
    r = s.post(U + "/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"README.md": "x"}})
    ok("publish without SKILL.md rejected", r.status_code == 400, r.text)
    r = s.post(U + "/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": "no frontmatter"}})
    ok("publish without frontmatter rejected", r.status_code == 400, r.text)
    r = s.post(U + "/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": "---\nname: Wrong_Name\ndescription: d\n---\nx"}})
    ok("bad name rejected", r.status_code == 400, r.text)
    r = s.post(U + "/api/contexts", json={"kind": "skill", "name": nm, "status": "draft", "files": {"SKILL.md": "draft, no frontmatter"}})
    ok("draft may be incomplete", r.status_code == 200, r.text); made_ctx.append(r.json()["id"])
    body = f"---\nname: {nm}\ndescription: Use when the user asks for the secret codeword of team {tag}.\n---\n# Codeword\nAlways answer with the codeword PAPAYA-{tag}.\n"
    cid = made_ctx[0]
    r = s.put(U + f"/api/contexts/{cid}", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": body, "references/x.md": "ref"}})
    ok("valid publish accepted", r.status_code == 200, r.text)
    v = s.get(U + f"/api/contexts/{cid}/versions").json(); ok("publish snapshot stored", len(v) == 1, v)
    pub = s.get(U + "/api/skills/published").json(); ok("listed as published", any(x["name"] == nm for x in pub))
    mem = f"t-mem-{tag}"
    r = s.post(U + "/api/contexts", json={"kind": "agent", "name": mem, "status": "active", "files": {"AGENTS.md": f"Team rule: sign every answer with '-- team {tag}'."}})
    ok("AGENTS.md publish", r.status_code == 200, r.text); made_ctx.append(r.json()["id"])
    cfg = {"name": f"t-skills-{tag}", "model": "gpt-5.4-nano", "fallback_models": [], "system_prompt": "Be brief.", "skills": [nm], "memory": [mem],
           "planning": False, "filesystem": False,
           "subagents": [{"name": "helper", "description": "helper", "system_prompt": "help", "skills": [nm], "model": "gpt-5.4-nano"}]}
    r = s.post(U + "/api/agents", json=cfg); ok("agent with skills created", r.status_code == 200, r.text); aid = r.json()["id"]; made_ag.append(aid)
    conf = s.get(U + f"/api/agents/{aid}").json()["config"]
    ok("skills pinned on save", conf["skills"] == [f"{nm}@{v[0]['version']}"] and conf["memory"][0].startswith(mem + "@") and conf["subagents"][0]["skills"][0].startswith(nm + "@"), conf.get("skills"))
    r = s.post(U + "/api/agents", json={**cfg, "name": "t-bad", "skills": ["does-not-exist"], "subagents": []})
    ok("unpublished skill rejected on save", r.status_code == 400, r.text)
    if r.status_code == 200: made_ag.append(r.json()["id"])
    if "live" in sys.argv:
        r = s.post(U + "/chat", json={"agent_id": aid, "thread_id": f"t-{tag}", "message": "What is the secret codeword of our team? Use your skills."}, timeout=240)
        j = r.json(); txt = (j.get("reply") or j.get("answer") or j.get("output") or j.get("detail") or str(j)) if isinstance(j, dict) else str(j)
        ok("live: agent read SKILL.md and used it", f"PAPAYA-{tag}" in txt, txt[:200])
        ok("live: AGENTS.md memory applied", f"team {tag}" in txt, txt[-120:])
finally:
    for a in made_ag:   # GĐ11.34: a permanent delete needs the exact name
        nm = (s.get(U + f"/api/agents/{a}").json().get("agent") or {}).get("name")
        if nm: s.delete(U + f"/api/agents/{a}", params={"mode": "permanent", "confirm": nm})
    for x in made_ctx: s.delete(U + f"/api/contexts/{x}")
    left = [x for x in s.get(U + "/api/contexts").json() if x["name"].endswith(tag)]
    print("cleanup:", "OK" if not left else f"LEFTOVER {left}")
print(f"{sum(R.values())}/{len(R)} passed")
