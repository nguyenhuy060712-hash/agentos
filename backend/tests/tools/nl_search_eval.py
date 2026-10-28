"""GD7.2 / 7.R: accuracy of natural-language trace search against a LIVE model. Sends every question of tests/data/nl_search_gold.jsonl
(20 VI + 20 EN) to POST /api/obs/nl-filter WITHOUT the fake-LLM "[spec]" hint and compares the returned filter with the expected DSL
(timestamps masked) and since_hours / mode / sort. Prints exact-match accuracy overall and per language, plus every miss.
Usage: python nl_search_eval.py BASE PROJECT_ID [--lang vi|en] [--limit N]    (credentials from env AOS_USER / AOS_PW, never printed)
Spends real model money when the server runs a real model (about 40 small calls, feature 'search'): run it only from the 7.R real check."""
import argparse, json, os, re, sys
from pathlib import Path
import httpx
ISO = re.compile(r'"\d{4}-\d\d-\d\dT[^"]*"')
GOLD = Path(__file__).resolve().parent.parent / "data" / "nl_search_gold.jsonl"

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("project_id", type=int)
    ap.add_argument("--lang", choices=["vi", "en"]); ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    rows = [json.loads(x) for x in GOLD.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if not a.lang or r["lang"] == a.lang][: a.limit]
    c = httpx.Client(base_url=a.base.rstrip("/"), auth=(os.environ["AOS_USER"], os.environ["AOS_PW"]), timeout=120)
    ok = {"vi": [0, 0], "en": [0, 0]}; miss = []
    for r in rows:
        resp = c.post("/api/obs/nl-filter", json={"project_id": a.project_id, "question": r["question"], "lang": r["lang"]})
        ok[r["lang"]][1] += 1
        if resp.status_code != 200: miss.append((r["id"], r["question"], f"HTTP {resp.status_code}: {resp.text[:120]}")); continue
        j = resp.json(); got = ISO.sub('"{T}"', j["filter"]); s = r["spec"]
        if got == r["dsl"] and j["since_hours"] == s["since_hours"] and j["mode"] == s["mode"] and j["sort"] == s["sort"]: ok[r["lang"]][0] += 1
        else: miss.append((r["id"], r["question"], f"got {got!r} since={j['since_hours']} mode={j['mode']} sort={j['sort']} | want {r['dsl']!r}"))
    for lang, (g, n) in ok.items():
        if n: print(f"{lang}: {g}/{n} = {100 * g / n:.0f}%")
    g, n = sum(v[0] for v in ok.values()), sum(v[1] for v in ok.values())
    print(f"TOTAL: {g}/{n} = {100 * g / max(n, 1):.0f}%")
    for m in miss: print("MISS", *m)
    return 0 if g == n else 1

if __name__ == "__main__": sys.exit(main())
