"""GĐ11.25 OAuth engine upgrade + per-provider redirect (F1). `oauth_providers` gets: grant_type ('authorization_code' | 'client_credentials'), issuer (RFC 9207 `iss` to expect),
resource (RFC 8707), token_auth now body | post (= body) | basic | none, token_params (extra token-request fields, not secret), token_headers_enc (extra token-endpoint headers, SEALED,
write-only), allowed_scopes (ceiling for requested scopes), default_redirect_uri (own redirect URI: https, or http only on 127.0.0.1 / [::1]), redirect_mode
('web' | 'loopback_paste' | 'tunnel'), redirect_base (AOS_PUBLIC_BASE a tunnel provider was last registered for), source ('manual' | 'catalog' | 'mcp'), iss_supported.
`oauth_states` gets the owner the flow was started for + workspace + issuer / resource / scope / redirect_uri snapshots (so a provider edit mid-flow cannot change what the callback checks).
The oauth_states table is created here when missing (a fresh DB runs migrations before oauth_tools.init) in the shape oauth_tools.DDL creates.
Existing rows keep behaving exactly as before (all defaults = the old behaviour). down(): drops the new columns."""
VERSION = "0022"
NAME = "oauth_provider_v2"

_STATES = """CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY, provider_id INT, user_id INT, verifier_enc TEXT, next TEXT,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL)"""
_PROV = ["grant_type TEXT NOT NULL DEFAULT 'authorization_code'", "issuer TEXT", "resource TEXT", "token_params JSONB NOT NULL DEFAULT '{}'", "token_headers_enc TEXT",
         "allowed_scopes TEXT", "default_redirect_uri TEXT", "redirect_mode TEXT NOT NULL DEFAULT 'web'", "redirect_base TEXT", "source TEXT NOT NULL DEFAULT 'manual'",
         "iss_supported BOOLEAN NOT NULL DEFAULT false"]
_STATE_COLS = ["owner_type TEXT NOT NULL DEFAULT 'user'", "owner_id INT", "workspace_id INT", "issuer TEXT", "resource TEXT", "scope TEXT", "redirect_uri TEXT"]
_CHECKS = (("oauth_providers_grant_type_chk", "grant_type IN ('authorization_code','client_credentials')"),
           ("oauth_providers_redirect_mode_chk", "redirect_mode IN ('web','loopback_paste','tunnel')"),
           ("oauth_providers_source_chk", "source IN ('manual','catalog','mcp')"),
           ("oauth_providers_token_auth_chk", "token_auth IN ('body','post','basic','none')"))

async def up(q):
    await q(_STATES)
    await q("ALTER TABLE oauth_providers " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _PROV))
    await q("ALTER TABLE oauth_states " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _STATE_COLS))
    await q("UPDATE oauth_states s SET owner_id = s.user_id, workspace_id = p.workspace_id FROM oauth_providers p WHERE p.id = s.provider_id AND s.owner_id IS NULL")
    for name, cond in _CHECKS:
        await q(f"ALTER TABLE oauth_providers DROP CONSTRAINT IF EXISTS {name}")
        await q(f"ALTER TABLE oauth_providers ADD CONSTRAINT {name} CHECK ({cond})")
    await q("ALTER TABLE oauth_states DROP CONSTRAINT IF EXISTS oauth_states_owner_type_chk")
    await q("ALTER TABLE oauth_states ADD CONSTRAINT oauth_states_owner_type_chk CHECK (owner_type IN ('user','agent','workspace'))")

async def down(q):
    await q("ALTER TABLE oauth_states DROP CONSTRAINT IF EXISTS oauth_states_owner_type_chk")
    await q("ALTER TABLE oauth_states " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _STATE_COLS))
    for name, _ in _CHECKS: await q(f"ALTER TABLE oauth_providers DROP CONSTRAINT IF EXISTS {name}")
    # a client_credentials provider cannot exist in the old shape (no user flow, no refresh token): its workspace-owned credentials go with it
    await q("DELETE FROM oauth_providers WHERE grant_type = 'client_credentials'")
    await q("UPDATE oauth_providers SET token_auth = 'body' WHERE token_auth IN ('post','none')")
    await q("ALTER TABLE oauth_providers " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _PROV))

async def plan(q):
    return ["oauth_providers: ADD grant_type (default authorization_code), issuer, resource, token_params, token_headers_enc, allowed_scopes, default_redirect_uri, "
            "redirect_mode (default web), redirect_base, source (default manual), iss_supported; CHECKs on grant_type / redirect_mode / source / token_auth; existing rows unchanged",
            "oauth_states: ADD owner_type (default user), owner_id, workspace_id, issuer, resource, scope, redirect_uri; owner_id / workspace_id back-filled from the provider"]
