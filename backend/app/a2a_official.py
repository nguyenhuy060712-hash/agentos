"""GĐ9.6 A2A 1.0 on the official `a2a-sdk` (1.2.3) — replaces the hand-made A2A of ext.py.
Per agent: agent card `GET /a2a/{id}/.well-known/agent-card.json` (A2A 1.0 + the SDK's v0.3 compat fields), JSON-RPC `POST /a2a/{id}`
(SendMessage / SendStreamingMessage / GetTask / ListTasks / CancelTask / SubscribeToTask; the old `message/send`, `message/stream`,
`tasks/get`, `tasks/cancel` names still work: SDK v0.3 compat, deprecated). `GET /a2a` lists the cards the caller may see.
Auth = the normal middleware (user API key aos_... as Bearer / session / Basic). Access = agent_sharing (invisible -> 404, no 'use' right -> 403);
demo accounts are refused (like /mcp/agents/{id}). Tasks persist in Postgres (a2a_tasks, RLS by workspace) and are private to the caller + agent.
Each task runs the agent through the normal run path (deepagent_api.run_agent: traced, costed, budget-checked) AS the calling user."""
import json, uuid
from fastapi.responses import JSONResponse
from google.protobuf import json_format
from psycopg.types.json import Jsonb
from starlette.routing import Route
from a2a.auth.user import User
from a2a.helpers.proto_helpers import get_message_text, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill, HTTPAuthSecurityScheme, ListTasksResponse,
                               SecurityRequirement, SecurityScheme, StringList, Task, TaskState, TaskStatus)

PROTOCOL = "1.0"

class _User(User):
    def __init__(self, uid, name): self._uid, self._name = uid, name
    @property
    def is_authenticated(self) -> bool: return self._uid is not None
    @property
    def user_name(self) -> str: return str(self._uid if self._uid is not None else "")

class _Ctx(DefaultServerCallContextBuilder):
    def build(self, request) -> ServerCallContext:
        import admin
        u = admin.cur() or {}
        c = super().build(request)
        c.user = _User(u.get("id"), u.get("username"))
        c.state["agent_id"] = int(request.path_params["aid"])
        c.state["headers"] = {k: v for k, v in c.state.get("headers", {}).items() if k.lower() not in ("authorization", "cookie", "x-api-key")}   # A2A-Version stays; credentials never travel on
        return c

def _aid(ctx: ServerCallContext) -> int: return int(ctx.state["agent_id"])
def _uid(ctx: ServerCallContext): return int(ctx.user.user_name) if ctx.user.user_name else None

class PgTaskStore(TaskStore):
    """a2a_tasks table; a task is visible only to the user who created it, through the agent it belongs to."""
    async def save(self, task: Task, context: ServerCallContext) -> None:
        import deepagent_api as A, admin
        await A.q("INSERT INTO a2a_tasks(id,agent_id,context_id,state,history,artifacts,workspace_id,owner_id,task) VALUES(%s,%s,%s,%s,'[]','[]',%s,%s,%s)"
                  " ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state, task=EXCLUDED.task, updated=now() WHERE a2a_tasks.agent_id=EXCLUDED.agent_id",
                  (task.id, _aid(context), task.context_id, TaskState.Name(task.status.state), admin.cur_ws(), _uid(context), Jsonb(json_format.MessageToDict(task))))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        import deepagent_api as A
        r = await A.q("SELECT task FROM a2a_tasks WHERE id=%s AND agent_id=%s AND owner_id=%s AND task IS NOT NULL", (task_id, _aid(context), _uid(context)), one=True)
        return json_format.ParseDict(r["task"], Task(), ignore_unknown_fields=True) if r else None

    async def list(self, params, context: ServerCallContext) -> ListTasksResponse:
        import deepagent_api as A
        w, a = ["agent_id=%s", "owner_id=%s", "task IS NOT NULL"], [_aid(context), _uid(context)]
        if params.context_id: w.append("context_id=%s"); a.append(params.context_id)
        if params.status: w.append("state=%s"); a.append(TaskState.Name(params.status))
        n = min(params.page_size if params.HasField("page_size") else 50, 100)
        rows = await A.q(f"SELECT task FROM a2a_tasks WHERE {' AND '.join(w)} ORDER BY updated DESC, id LIMIT %s", (*a, n))
        tot = await A.q(f"SELECT count(*) c FROM a2a_tasks WHERE {' AND '.join(w)}", tuple(a), one=True)
        return ListTasksResponse(tasks=[json_format.ParseDict(r["task"], Task(), ignore_unknown_fields=True) for r in rows], total_size=tot["c"], page_size=len(rows))

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        import deepagent_api as A
        await A.q("DELETE FROM a2a_tasks WHERE id=%s AND agent_id=%s AND owner_id=%s", (task_id, _aid(context), _uid(context)))

class Executor(AgentExecutor):
    async def execute(self, context, event_queue) -> None:
        import deepagent_api as A, agent_mcp
        aid = _aid(context.call_context)
        text = get_message_text(context.message).strip()
        up = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:   # first message of a task: the SDK wants the Task itself before any status update
            await event_queue.enqueue_event(Task(id=context.task_id, context_id=context.context_id, status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED), history=[context.message]))
        await up.start_work()
        if not text:
            return await up.failed(new_text_message("message.parts needs at least one text part"))
        art, last, buf = f"{context.task_id}-answer", {}, False
        async for e in A.run_agent(aid, await A.cur_version(aid), f"a2a-{context.context_id}", text[:8000], tags=["a2a"], tokens=True):
            if e["type"] == "token" and e.get("text"):
                await up.add_artifact([new_text_part(e["text"])], artifact_id=art, name="answer", append=buf, last_chunk=False); buf = True
            elif e["type"] == "done": last = e
        if last.get("error") and not last.get("paused"): return await up.failed(new_text_message(str(last["error"])[:500]))
        reply = last.get("reply") or ""
        if not buf and reply: await up.add_artifact([new_text_part(reply)], artifact_id=art, name="answer", last_chunk=True)
        elif buf: await up.add_artifact([new_text_part("")], artifact_id=art, name="answer", append=True, last_chunk=True)
        if last.get("paused"): return await up.requires_input(new_text_message(agent_mcp.reply_text(last)))
        await up.complete(new_text_message(reply) if reply else None)

    async def cancel(self, context, event_queue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

_D = None
def dispatcher() -> JsonRpcDispatcher:
    global _D
    if _D is None:
        generic = AgentCard(name="Agent OS", description="Moza Agent OS agents", version="1", capabilities=AgentCapabilities(streaming=True, push_notifications=False),
                            supported_interfaces=[AgentInterface(url="http://localhost/a2a", protocol_binding="JSONRPC", protocol_version=PROTOCOL)],
                            default_input_modes=["text/plain"], default_output_modes=["text/plain"], skills=[AgentSkill(id="agent", name="agent", description="Agent", tags=["agent-os"])])
        _D = JsonRpcDispatcher(DefaultRequestHandler(Executor(), PgTaskStore(), generic), context_builder=_Ctx(), enable_v0_3_compat=True)
    return _D

def card(base: str, a: dict) -> AgentCard:
    desc = a.get("description") or f"Agent {a['name']} (Moza Agent OS)"
    return AgentCard(name=a["name"], description=desc, version=str(a["current_version"]),
                     supported_interfaces=[AgentInterface(url=f"{base}/a2a/{a['id']}", protocol_binding="JSONRPC", protocol_version=PROTOCOL)],
                     capabilities=AgentCapabilities(streaming=True, push_notifications=False),
                     default_input_modes=["text/plain"], default_output_modes=["text/plain"],
                     security_schemes={"bearer": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer", description="Agent OS API key (aos_...) or session"))},
                     security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
                     skills=[AgentSkill(id=f"agent-{a['id']}", name=a["name"], description=desc, tags=["agent-os"], examples=[])])

async def _row(aid: int):
    import deepagent_api as A
    return await A.q("SELECT a.id, a.name, a.current_version, v.config->>'description' description FROM agents a"
                     " JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s AND NOT a.deleted AND NOT a.hidden", (aid,), one=True)

async def _gate(aid: int):
    """-> (agent row, None) or (None, JSONResponse)."""
    import agent_mcp
    a = await _row(aid)
    if not a: return None, JSONResponse({"detail": "Agent not found"}, 404)
    bad = await agent_mcp._gate(aid)
    return (None, JSONResponse({"detail": bad[1]}, bad[0])) if bad else (a, None)

def mount(app):
    import ext
    async def card_ep(request):
        a, err = await _gate(int(request.path_params["aid"]))
        return err or JSONResponse(_card_json(ext._base(request), a))
    def _card_json(base, a):   # A2A 1.0 card + the v0.3 top-level fields (url / protocolVersion / preferredTransport) so older A2A clients still read it
        d = agent_card_to_dict(card(base, a))
        d.setdefault("url", f"{base}/a2a/{a['id']}"); d.setdefault("protocolVersion", "0.3.0"); d.setdefault("preferredTransport", "JSONRPC")
        return d
    async def list_ep(request):
        import deepagent_api as A
        rows = await A.q("SELECT a.id, a.name, a.current_version, v.config->>'description' description FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version"
                         " WHERE NOT a.deleted AND NOT a.hidden ORDER BY a.id")
        return JSONResponse([_card_json(ext._base(request), r) for r in rows])
    async def rpc_ep(request):
        a, err = await _gate(int(request.path_params["aid"]))
        return err or await dispatcher().handle_requests(request)
    routes = [Route("/a2a/{aid:int}/.well-known/agent-card.json", card_ep, methods=["GET"]), Route("/a2a", list_ep, methods=["GET"]),
              Route("/a2a/{aid:int}", rpc_ep, methods=["POST"]), Route("/a2a/{aid:int}/", rpc_ep, methods=["POST"])]
    for r in reversed(routes): app.router.routes.insert(0, r)
