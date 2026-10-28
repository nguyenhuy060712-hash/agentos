"""GĐ9.Q wiring (server / auth / sandbox / builder / gateway parity): one place that creates the tables and mounts every module, so deepagent_api.py only
carries three lines (init before RLS, init_late after the sandbox tables exist, mount at the end).
Modules: srv_parity (P5 headers, logs, cache, checkpointer, OpenAPI, docs) · lg_auth (P7 custom auth + OIDC provider) · agent_identity (P4 identity, audit, auth-aware tools)
· custom_apps (P4) · sbx_parity (P7 sandboxes) · gw_policies (P6 per-customer policies, decision models, routing) · ui_parity (pages)."""
import caller, srv_parity, lg_auth, agent_identity, custom_apps, sbx_parity, gw_policies

async def init():
    await srv_parity.init(); await lg_auth.init(); await agent_identity.init(); await custom_apps.init(); await gw_policies.init()

async def init_late():
    await sbx_parity.init_late()

async def _authn(request): return await lg_auth.authenticate_hook(request)

async def _authz(request, u):
    r = await agent_identity.enrich_and_restrict(request, u)
    if r is not None: return r
    return await lg_auth.authorize_hook(request, u)

def mount(app):
    import admin, ui
    for m in (srv_parity, lg_auth, agent_identity, custom_apps, sbx_parity, gw_policies): app.include_router(m.router)
    admin.EXT_AUTHN = _authn; admin.EXT_AUTHZ = _authz
    app.add_middleware(srv_parity.HeadersMiddleware)       # added last = outermost: sees every request, even those the auth middleware refuses
    srv_parity.install_openapi(app)
    import ui_parity; ui.PAGES.update(ui_parity.PAGES)
