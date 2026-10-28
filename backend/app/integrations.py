"""GĐ12.85 "Integrations" page data (view-only): one row per Google / Microsoft tool family with the CALLER's own connection state. Assembled from
connections._states (status, account, granted scope), workspace_tools.modes (off | read | write) and the office tool registry (workspace_tools.META). Nothing is written, no token or
refresh token is ever read into the answer (only status / account / scope names). The demo account and channel identities get an empty list. Controls stay in Settings (modes, Connections)."""
from pathlib import Path
from fastapi import APIRouter

router = APIRouter(tags=["integrations"])
STATES = ("connected", "reconnect", "missing_scope", "not_configured", "off")
GUIDE = Path(__file__).parent / "static" / "guides" / "HUONG_DAN_KET_NOI_GOOGLE_MICROSOFT.pdf"
# (office_scopes provider, workspace_tools kind) and the label of each family (brand names are not translated)
PROVIDERS = (("google", "google"), ("microsoft", "ms365"))
FAMILY_LABELS = {("google", "gmail"): "Gmail", ("google", "calendar"): "Google Calendar", ("google", "docs"): "Google Docs", ("google", "sheets"): "Google Sheets",
                 ("google", "drive"): "Google Drive", ("microsoft", "mail"): "Outlook", ("microsoft", "calendar"): "Outlook Calendar",
                 ("microsoft", "files"): "OneDrive, SharePoint, Word, Excel, PowerPoint"}

def state_of(provider, family, mode, row) -> str:
    """The one state of a family: off (workspace mode) > not_configured (no provider / not connected) > reconnect (token no longer works) > missing_scope > connected."""
    import office_scopes as OS
    if mode not in ("read", "write"): return "off"
    if not row or not row.get("connected"): return "not_configured"
    if row.get("status") != "active": return "reconnect"
    if OS.missing_from(row.get("scope"), OS.need(provider, family, write=(mode == "write"))): return "missing_scope"
    return "connected"

def tools_of(kind, family) -> list:
    import workspace_tools as W
    return [{"key": m.key, "label": W.LABELS.get(m.key, m.name), "tier": m.tier} for m in W.META.values() if m.kind == kind and m.family == family and not m.alias_of]

@router.get("/api/integrations")
async def integrations():
    import admin, connections as C, office_scopes as OS, workspace_tools as W
    u = admin.cur() or {}
    if not u.get("id") or admin.is_demo(u) or u.get("auth") == "channel": return []
    import deepagent_api as A
    ws = int(u.get("ws") or 1)
    if ws in A.gw.DEMO_WS: return []
    modes = await W.modes(ws)
    rows = {r["provider"]: r for r in await C._states(("user", u["id"]), ws) if r.get("provider") in ("google", "microsoft")}
    can_manage = u.get("role") != "viewer"
    guide = "/static/guides/" + GUIDE.name if GUIDE.exists() else None
    out = []
    for provider, kind in PROVIDERS:
        row = rows.get(provider)
        for family in OS.FAMILIES[provider]:
            st = state_of(provider, family, modes.get(kind, "off"), row)
            out.append({"provider": provider, "family": family, "label": FAMILY_LABELS.get((provider, family), family), "state": st, "mode": modes.get(kind, "off"),
                        **({"account": row["account"]} if row and row.get("connected") and row.get("account") else {}), "tools": tools_of(kind, family),
                        "connect_url": W.FIXED_URL, "can_manage": can_manage, "guide_url": guide})
    return out
