# Agent OS: UX audit inside each page (compared with the LangSmith docs, langsmith_ui_spec.md and the internal ls_ref)
Date: 2026-10-19 (Asia/Bangkok). Severity: P0 = workflow broken; P1 = missing or wrong core step / dead action; P2 = polish.
Status: FIXED = deployed. LEFT = still open.

| # | Page / step | Issue | Type | Sev | Status |
|---|---|---|---|---|---|
| 1 | Evaluators › editor | Crashed with "models.map is not a function" because api/models returns an object | non-working | P0 | FIXED |
| 2 | Playground › compare models | Only one model listed (same api/models bug) | wrong data | P0 | FIXED |
| 3 | Build › new agent | Model picker empty (same bug) | wrong data | P0 | FIXED |
| 4 | Build › agent row | Opened the classic page, so there was no in-style configure page | misplaced | P1 | FIXED: builder?id= with Configure / Test / Versions / Deployments tabs |
| 5 | Build › configure | No in-style editing of instructions, model, temperature, tools, approvals, capabilities or budgets | missing | P1 | FIXED |
| 6 | Build › test | No chat test in the new UI | missing | P1 | FIXED: streaming chat via /chat/stream, with steps, latency, tokens, cost and a trace link |
| 7 | Build › deploy | Deploy was only reachable through Deployments | missing | P1 | FIXED: Deploy slide-over (name + revision), key shown once, link to the deployment |
| 8 | Build › versions | No revision history or restore | missing | P1 | FIXED |
| 9 | Annotation queue › review | Review linked to the classic page; no rubric, hotkeys or next/skip | missing | P1 | FIXED: 3-pane reviewer, rubric (bool/categorical/numeric), notes and correction, S/D/V/Ctrl+Enter/1-9, "All caught up" state |
| 10 | Annotation queue › list | Queues could not be deleted | dead action | P2 | FIXED |
| 11 | Evaluators › list | Rows were not clickable; creating from a template went nowhere | dead button | P1 | FIXED: rows and templates open the editor |
| 12 | Evaluators › bind | No dataset binding or online (project) binding from the evaluator | missing | P1 | FIXED: dataset checkboxes (PATCH config) and a "Bind to project" rule pane with sampling and backfill |
| 13 | Evaluators › scores | No feedback score history | missing | P1 | FIXED: Recent scores (/api/ui/feedback_recent) |
| 14 | Prompts › webhooks | Trigger, header and prompt filters were not stored | non-working | P1 | FIXED |
| 15 | Deployments › CLI | Placeholder snippet | placeholder | P1 | FIXED: real curl with X-API-Key |
| 16 | Datasets › new experiment | Fake SDK snippet | placeholder | P1 | FIXED: honest REST snippet |
| 17 | Classic pages | Vietnamese hard-coded strings in EN mode (chat errors, tools safety note, span tree, studio generate, queue empty) | i18n | P1 | FIXED |
| 18 | Evaluator editor | Field label "Models" should read "Model" | copy | P2 | FIXED (stale row: the editor already used "Model"; checked by roadmap_test p2) |
| 19 | Datasets › examples | Add/edit/CSV/tag/split work but still partly in the classic editor | misplaced | P1 | DONE (55b99b1: in-style add/edit/delete, CSV import + column mapping, split filter, add-from-traces) |
| 20 | Experiments › single view | No heat-mapped per-example score grid | missing | P1 | DONE (55b99b1: heatmap grid + key picker + regressions filter) |
| 21 | Tracing › filter | No ?agent= deep-link filter (the Traces button goes to the agent Overview) | missing | P2 | FIXED (Traces button now opens projects?agent=ID, prefiltered on metadata.agent_id) |
| 22 | Sandboxes / LLM Gateway keys | Detail slide-overs are thin (no usage chart per key) | missing | P2 | FIXED (LLM Gateway: virtual-keys table plus per-key slide-over with 14-day daily calls/errors chart, by-model table, and new endpoint GET /api/gw/keys/{id}/usage. Sandbox detail tabs shipped in roadmap step 4) |
| 23 | Studio | Graph and thread view work; no time-travel or fork | missing | P2 | FIXED (stale row: Studio already had checkpoint history, state at a checkpoint, and fork. Verified end to end by roadmap_test p2: a fork from a past checkpoint answers APPLE, not BANANA) |
| 24 | Deployments detail | Revisions, logs and threads exist; no assistants list | missing | P2 | FIXED (Assistants tab, roadmap step 1, 4beb256) |
| 25 | Global | Mixed card padding and table density on older classic pages | styling | P2 | FIXED (one padding/density standard for classic pages via body.cls CSS) |

E2E: os/tests/workflow_e2e.py drives the main workflows and saves step screenshots to /workspace/adk_ha_build/shots_wf/<workflow>_<NN>_<step>_<lang>.png.

## Update 17:10 (Asia/Bangkok)
| # | Page / step | Issue | Sev | Status |
|---|---|---|---|---|
| 19 | Datasets › examples | Add/edit/delete example, CSV import with column mapping, split filter, "Add from traces" (no classic links) | P1 | FIXED |
| 20 | Experiments › compare/single view | Score-key picker, heatmap for a single experiment, "Regressions only" filter | P1 | FIXED |
| 26 | Settings › Model pricing | Showed "[object Object]" | P1 | FIXED |
| 27 | Compare API | Vietnamese error text in EN | P1 | FIXED |
| 28 | Tracing › HITL | Resumed run lands in a separate trace | P2 | DONE (77443a3) |
| 29 | Gateway | OpenAI provider, $1 hard cap, GET /api/gateway/caps | P0 (owner) | FIXED |
| 30 | Build › tools | web_search / fetch_url tools for research agents | P1 | FIXED |
