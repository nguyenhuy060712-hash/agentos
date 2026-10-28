# LLM Gateway: per-customer policies, decision models, routing

## Per-customer policies (`X-Gateway-*` headers)

Your backend authenticates its end customer and sends `X-Gateway-Customer-Id: acme` with the gateway key. The gateway matches only `X-Gateway-*` headers and keys of the JSON header `X-Gateway-Metadata`;
names are normalised (prefix removed, lower-case, anything outside `a-z0-9_` becomes `_` → `Customer-Id`, `customer_id` and `CUSTOMER.ID` are all `customer_id`), values are compared exactly.
Identity names (`workspace_id`, `user_id`, `user_email`, `api_key_id`, …) and anything starting with `gateway` are discarded.

* **Separate a default limit by a header** — one spend (USD / month) or rate (requests / minute) policy; every value gets its own bucket, requests without the header share a fallback bucket.
* **Explicit condition** — one header = one value.
* Every matching policy is enforced; a policy can be pinned to one key; scope `key` (per key) or `workspace` (all keys together).
* Policies: `GET/POST/PATCH/DELETE /api/gw/policies` (admin).

LangSmith warns that a caller who controls both the key and the header can pick its own limit. Agent OS closes this with a **per-key header policy** (`PUT /api/gw/keys/{id}/header-policy`): `allow` (which headers a client may set), `strip`, and `inject` (values the operator fixes: a key issued to acme always counts as `customer_id=acme`).

## Decision models

`POST /v1/decisions` classifies or scores text and returns structured answers instead of a chat message:

```bash
curl https://<host>/v1/decisions -H "Authorization: Bearer vk-…" -H 'content-type: application/json' -d '{
  "input": "The package arrived with a broken screen.",
  "questions": [
    {"type": "predicate", "name": "damaged", "instructions": "Does the customer report a damaged item?"},
    {"type": "choice", "name": "topic", "instructions": "Main topic", "options": ["shipping", "product", "billing"]},
    {"type": "score", "name": "anger", "instructions": "How angry is the customer?", "scale": [0, 10]}]}'
# {"model": "...", "answers": [{"type":"predicate","name":"damaged","probability":0.97}, {"type":"choice","name":"topic","choice":"shipping"}, {"type":"score","name":"anger","score":6}], "usage": {...}}
```
Runs on a small Gemini model (default `gemini-flash-lite-latest`), under the key's budget, rate limits, PII mode and `X-Gateway-*` policies. The text is treated as data; answers are validated and clamped (an invalid answer is reported as `error`, never guessed).

## Routing: `model: "auto"`

A key with a routing policy (`PUT /api/gw/keys/{id}/routing`) lets clients ask for `auto`:

```json
{"mode": "decision", "objective": "cost", "decision_model": "gemini-flash-lite-latest", "max_latency_ms": 4000,
 "tiers": [{"name": "fast", "model": "gemini-flash-lite-latest"}, {"name": "balanced", "model": "gemini-3.6-flash"}, {"name": "strong", "model": "gemini-3.8-flash"}],
 "rules": [{"if": {"has_images": true}, "tier": "balanced"}, {"if": {"max_chars": 300}, "tier": "fast"}], "default_tier": "balanced"}
```
* **rules**: first match on size, tools, images, keywords; otherwise `default_tier`.
* **decision**: a small model rates the request (`complexity`: simple / moderate / hard; `needs_reasoning`) → fast / balanced / strong; if it fails, the rules answer.
* **objective** `cost` moves one tier down, `quality` one tier up; **`max_latency_ms`** moves down while the tier's measured p50 (24 h, this gateway's own traffic) is above the budget; the key's model allow-list always wins.
* The response carries `x-agentos-model` and `x-agentos-route` (tier, reason). Dry run: `POST /api/gw/route/test`.
