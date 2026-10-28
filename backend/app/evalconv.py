"""GĐ12.70: judge a whole conversation. `build(turns, tools)` turns the root runs of a thread (+ the tool child runs of each turn) into the one dict the conversation / trajectory
judges (evalcat) read: all_messages (de-duplicated when a turn repeats the history), human_ai_pairs, first_human_last_ai, trajectory (tool calls + results), transcript."""
import json

_ROLE = {"human": "human", "user": "human", "ai": "ai", "assistant": "ai", "agent": "ai"}
_LABEL = {"human": "Người dùng", "ai": "Agent"}

def _text(c):
    if isinstance(c, list): c = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)

def _input_messages(inputs):
    """Messages a chat run received: `messages` (history repeated) or the single `message`/`question`/... text."""
    if not isinstance(inputs, dict): return [{"role": "human", "content": _text(inputs)}] if inputs else []
    ms = inputs.get("messages")
    if isinstance(ms, list) and ms:
        out = []
        for m in ms:
            if isinstance(m, dict):
                r = _ROLE.get(str(m.get("role") or m.get("type") or "human").lower())
                if r: out.append({"role": r, "content": _text(m.get("content", ""))})
            elif isinstance(m, str): out.append({"role": "human", "content": m})
        if out: return out
    for k in ("question", "message", "input", "q"):   # same order as evals._question (not imported: evals pulls in the whole app)
        if k in inputs: return [{"role": "human", "content": _text(inputs[k])}]
    return [{"role": "human", "content": next((v for v in inputs.values() if isinstance(v, str)), json.dumps(inputs, ensure_ascii=False))}]

def _merge(acc, new):
    """Append `new` to `acc` skipping the part already there: the longest k with acc[-k:] == new[:k] is the repeated history."""
    for k in range(min(len(acc), len(new)), 0, -1):
        if acc[-k:] == new[:k]: return acc + new[k:]
    return acc + new

def _s(x, n):
    return (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str))[:n]

def build(turns, tools=None, only_tools=None):
    """turns: root runs ordered by time ({id, inputs, outputs}); tools: tool child runs ({trace_id, name, inputs, outputs, status}) in time order;
    only_tools: keep only the tool calls with these names."""
    msgs = []
    for t in turns:
        new = _input_messages(t.get("inputs")); hist = len(new) > 1   # a single new message cannot be a repeated history
        ans = (t.get("outputs") or {}).get("answer") if isinstance(t.get("outputs"), dict) else None
        if ans not in (None, ""): new = new + [{"role": "ai", "content": _text(ans)}]
        msgs = _merge(msgs, new) if hist else msgs + new
    pairs, cur = [], None
    for m in msgs:
        if m["role"] == "human": cur = m["content"]
        elif cur is not None: pairs.append({"human": cur, "ai": m["content"]}); cur = None
    first_h = next((m["content"] for m in msgs if m["role"] == "human"), "")
    last_a = next((m["content"] for m in reversed(msgs) if m["role"] == "ai"), "")
    keep = set(only_tools) if only_tools else None
    traj = [{"name": x["name"], "args": (x.get("inputs") or {}).get("args", x.get("inputs")) if isinstance(x.get("inputs"), dict) else x.get("inputs"),
             "inputs": x.get("inputs"), "outputs": x.get("outputs"), "ok": x.get("status") != "error"}
            for x in (tools or []) if x.get("name") != "write_todos" and (keep is None or x["name"] in keep)]
    traj_text = "\n".join(f"{i + 1}. {s['name']}({_s(s['inputs'], 300)}) -> {_s(s['outputs'], 500)}" for i, s in enumerate(traj)) or "(no tool calls)"
    return {"all_messages": msgs, "human_ai_pairs": pairs, "first_human_last_ai": {"human": first_h, "ai": last_a}, "trajectory": traj, "trajectory_text": traj_text,
            "transcript": "\n".join(f"{_LABEL[m['role']]}: {m['content']}" for m in msgs)}
