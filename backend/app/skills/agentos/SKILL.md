---
name: agentos
description: Use when asked to inspect or improve an Agent OS deployment - find failing traces, read a conversation, check costs, read or propose prompt edits, build a dataset from traces, draft an evaluator, run or compare experiments. Dùng khi cần xem trace lỗi, hội thoại, chi phí, sửa prompt, tạo dataset, so sánh thí nghiệm trong Agent OS.
---

# Agent OS tools

Tools run as the signed-in user (workspace and privacy rules apply). Results are trimmed; if `next_offset` is not null, call again with `offset=next_offset`.

## Read (safe, any time)
| tool | use |
|---|---|
| `query_runs(project, filter, limit)` | newest traces. filter DSL: `eq(status,"error")`, `gt(latency,5)`, `search("refund")`, `and(...)` |
| `get_trace(trace_id)` | spans of one trace; `span_id=` to read one span in full |
| `get_thread(thread_id)` | the turns of a conversation |
| `list_issues(project, status, kind)` | Engine issues: recurring errors, low evaluator scores, thumbs-down; with root cause + proposal type when analysed, and sample run ids |
| `get_prompt(name, tag or commit)` | prompt text; tags show what production serves |
| `list_pack_versions(name)`, `get_pack(name, ref, path)` | Context Hub packs: commit history + tags (prod = what agents read), file list / one file at a tag or commit |
| `get_costs(period, group_by)` | spend by model / user / agent / feature; period `24h` `7d` `30d` `today` |
| `list_datasets`, `get_experiment(id)`, `compare_experiments(a, b)` | evaluation results |

## Write (ask the user first; each one only ADDS something)
- `propose_prompt_edit(name, new_template, message)` makes a new commit. It never moves the `production` tag - tell the user to review the diff and move the tag themselves.
- `create_dataset_from_runs(name, run_ids)` one example per run.
- `draft_evaluator(name, kind, prompt|code)` a draft; it scores nothing until attached.
- `run_experiment(dataset, target_agent)` starts a background run that costs model budget; then poll `get_experiment`.

## Workflow: "why is this agent failing?" / "agent này lỗi vì sao?"
1. `list_issues(project)` -> pick the biggest issue (it may already carry a root cause; the Engine page can apply the fix).
2. `get_trace(<a sample_run_id>)` -> find the failing span, then `get_prompt` of the agent's prompt.
3. Explain the cause in 2-3 sentences. If a prompt change would help: `propose_prompt_edit`, then `create_dataset_from_runs` with the failing run ids, then `run_experiment` to check the new commit.

## Examples
- EN: "Show errors from the last day in project agents" -> `query_runs("agents", 'and(eq(status,"error"))', 10)`.
- VI: "Tuần này tốn bao nhiêu tiền theo model?" -> `get_costs("7d", "model")`.
- VI: "Gom các trace lỗi thành dataset" -> `query_runs` lấy `id`, rồi `create_dataset_from_runs("loi-tuan-nay", [ids])`.

## Rules
- Quote ids exactly as returned; never invent trace, run or experiment ids.
- Do not paste long prompts or traces back to the user; summarise and point to the id.
- Never claim a prompt is live: new commits are proposals until a person moves a tag.
