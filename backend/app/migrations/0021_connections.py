"""GĐ11.24 connections model (F1, single owner). New table `connections` (a workspace's named connection: secret / oauth2 / mcp_oauth / client_credentials) and
`oauth_connections` becomes the credential store of ANY owner: owner_type 'user' | 'agent' | 'workspace' + owner_id (back-filled = user_id), connection_id, kind
('oauth' = access/refresh tokens as before, 'secret' = one sealed value in value_enc), status_reason, label. UNIQUE(provider_id,user_id) becomes
UNIQUE(provider_id, owner_type, owner_id) (+ a partial unique index for secret rows); user_id / provider_id / access_token_enc become nullable (agent and secret rows).
`aad_owner` (generated) is the owner part of the AAD of the sealed cells: for a user row it is the user id, exactly the key the cells were sealed with before
(old rows stay readable); other owners get 'agent-5' / 'workspace-0' so a user's cell can never be replayed as an agent's.
Every existing provider gets a `connections` row (slug = provider name) and its connections point to it. The two base tables are created here when missing
(a fresh DB runs migrations before oauth_tools.init) in the shape oauth_tools.DDL creates, so the ALTERs below always have something to alter.
down(): agent / workspace / secret rows are DELETED (they cannot exist in the old shape); user connections keep their tokens."""
VERSION = "0021"
NAME = "connections"

_BASE = ["""CREATE TABLE IF NOT EXISTS oauth_providers(id SERIAL PRIMARY KEY, name TEXT NOT NULL, display_name TEXT, authorize_url TEXT NOT NULL, token_url TEXT NOT NULL,
  revoke_url TEXT, userinfo_url TEXT, api_base TEXT NOT NULL, client_id TEXT NOT NULL, client_secret_enc TEXT, scopes TEXT NOT NULL DEFAULT '',
  extra_params JSONB NOT NULL DEFAULT '{}', token_auth TEXT NOT NULL DEFAULT 'body', pkce BOOLEAN NOT NULL DEFAULT true, enabled BOOLEAN NOT NULL DEFAULT true,
  workspace_id INT NOT NULL DEFAULT 1, created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name))""",
         """CREATE TABLE IF NOT EXISTS oauth_connections(id SERIAL PRIMARY KEY, provider_id INT REFERENCES oauth_providers(id) ON DELETE CASCADE, user_id INT NOT NULL,
  workspace_id INT NOT NULL DEFAULT 1, account TEXT, access_token_enc TEXT NOT NULL, refresh_token_enc TEXT, token_type TEXT, scope TEXT,
  expires_at TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'active', created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used TIMESTAMPTZ, last_refresh TIMESTAMPTZ, UNIQUE(provider_id, user_id))"""]

_CONNECTIONS = """CREATE TABLE IF NOT EXISTS connections(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, slug TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('secret','oauth2','mcp_oauth','client_credentials')), provider_id INT REFERENCES oauth_providers(id) ON DELETE CASCADE,
  mcp_url TEXT, display_name TEXT, allowed_scopes TEXT, profile BOOLEAN NOT NULL DEFAULT false, created_by INT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, slug))"""

_AAD = ("aad_owner TEXT GENERATED ALWAYS AS (CASE WHEN owner_type = 'user' THEN coalesce(owner_id, user_id)::text "
        "ELSE owner_type || '-' || coalesce(owner_id, 0)::text END) STORED")
_NEW_COLS = ["owner_type TEXT NOT NULL DEFAULT 'user'", "owner_id INT", "connection_id INT REFERENCES connections(id) ON DELETE CASCADE",
             "kind TEXT NOT NULL DEFAULT 'oauth'", "value_enc TEXT", "status_reason TEXT", "label TEXT", _AAD]
_GONE = ["aad_owner", "owner_type", "owner_id", "connection_id", "kind", "value_enc", "status_reason", "label"]   # drop order: generated column first

async def up(q):
    for s in _BASE: await q(s)
    await q(_CONNECTIONS)
    await q("ALTER TABLE oauth_connections " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _NEW_COLS[:-1]))
    await q("UPDATE oauth_connections SET owner_id = user_id WHERE owner_type = 'user' AND owner_id IS NULL")
    await q("ALTER TABLE oauth_connections ADD COLUMN IF NOT EXISTS " + _AAD)
    await q("UPDATE oauth_connections SET status = 'error' WHERE status NOT IN ('active','needs_reconnect','revoked','error')")
    await q("ALTER TABLE oauth_connections ALTER COLUMN user_id DROP NOT NULL, ALTER COLUMN access_token_enc DROP NOT NULL")
    await q("ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS oauth_connections_provider_id_user_id_key")
    await q("ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS oauth_connections_owner_key")
    await q("ALTER TABLE oauth_connections ADD CONSTRAINT oauth_connections_owner_key UNIQUE (provider_id, owner_type, owner_id)")
    await q("CREATE UNIQUE INDEX IF NOT EXISTS oauth_connections_secret_owner ON oauth_connections(connection_id, owner_type, owner_id) WHERE kind = 'secret'")
    for name, cond in (("oauth_connections_owner_type_chk", "owner_type IN ('user','agent','workspace')"),
                       ("oauth_connections_kind_chk", "kind IN ('oauth','secret')"),
                       ("oauth_connections_status_chk", "status IN ('active','needs_reconnect','revoked','error')"),
                       ("oauth_connections_user_chk", "owner_type <> 'user' OR user_id IS NOT NULL"),
                       ("oauth_connections_shape_chk", "kind <> 'oauth' OR (provider_id IS NOT NULL AND access_token_enc IS NOT NULL)")):
        await q(f"ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS {name}")
        await q(f"ALTER TABLE oauth_connections ADD CONSTRAINT {name} CHECK ({cond})")
    await q("""INSERT INTO connections(workspace_id, slug, kind, provider_id, display_name)
               SELECT workspace_id, name, 'oauth2', id, coalesce(display_name, name) FROM oauth_providers ON CONFLICT (workspace_id, slug) DO NOTHING""")
    await q("""UPDATE oauth_connections c SET connection_id = n.id FROM connections n
               WHERE n.provider_id = c.provider_id AND n.kind = 'oauth2' AND n.workspace_id = c.workspace_id AND c.connection_id IS NULL AND c.kind = 'oauth'""")

async def down(q):
    await q("DELETE FROM oauth_connections WHERE owner_type <> 'user' OR kind <> 'oauth' OR user_id IS NULL")
    for name in ("owner_type", "kind", "status", "user", "shape"): await q(f"ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS oauth_connections_{name}_chk")
    await q("DROP INDEX IF EXISTS oauth_connections_secret_owner")
    await q("ALTER TABLE oauth_connections DROP CONSTRAINT IF EXISTS oauth_connections_owner_key")
    for c in _GONE: await q(f"ALTER TABLE oauth_connections DROP COLUMN IF EXISTS {c}")
    await q("ALTER TABLE oauth_connections ALTER COLUMN user_id SET NOT NULL, ALTER COLUMN access_token_enc SET NOT NULL")
    await q("ALTER TABLE oauth_connections ADD CONSTRAINT oauth_connections_provider_id_user_id_key UNIQUE (provider_id, user_id)")
    await q("DROP TABLE IF EXISTS connections")   # its RLS policy goes with it

async def plan(q):
    have = bool(await q("SELECT 1 WHERE to_regclass('oauth_connections') IS NOT NULL"))
    n = (await q("SELECT count(*) FROM oauth_connections"))[0][0] if have else 0
    return [f"connections: CREATE TABLE (workspace_id, slug, kind, provider_id, mcp_url, display_name, allowed_scopes, profile, UNIQUE(workspace_id, slug)); one row per existing OAuth provider",
            f"oauth_connections ({n} rows): ADD owner_type (all 'user'), owner_id (= user_id), connection_id, kind, value_enc, status_reason, label, aad_owner (generated); "
            "UNIQUE(provider_id,user_id) -> UNIQUE(provider_id, owner_type, owner_id); user_id / access_token_enc become nullable; tokens are not touched"]
