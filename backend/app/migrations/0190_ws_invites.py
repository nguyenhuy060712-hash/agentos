"""GĐ12.100 workspace invites (link). One row per invite link; the link token is NEVER stored: `token_hash` = sha256 of the random token (secrets.token_urlsafe(32)).
  ws_invites: workspace_id -> workspaces ON DELETE CASCADE; role = the role the new member gets (never above the inviter's); email_note = the optional address the admin typed
  (a note on this row only: there is no email column on users, nothing is matched to it); expires_at / max_uses / used_count (the accept is one atomic
  UPDATE ... WHERE used_count < max_uses); revoked_at; created_by = the inviter's user id (re-checked when the link is used: a demoted / disabled / deleted inviter kills the link).
  The link is opened anonymously, but the accept runs in the system session; the table is still under workspace row-level security (admin.RLS_DIRECT), so a workspace session only ever
  sees its own invites, and every query in app/invites.py also filters by workspace_id explicitly. down(): drops the table."""
VERSION = "0190"
NAME = "ws_invites"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS ws_invites(
      id SERIAL PRIMARY KEY, workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL CHECK (role IN ('admin','editor','viewer')), email_note TEXT, created_by INT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
      max_uses INT NOT NULL DEFAULT 1 CHECK (max_uses BETWEEN 1 AND 20), used_count INT NOT NULL DEFAULT 0 CHECK (used_count >= 0 AND used_count <= max_uses), revoked_at TIMESTAMPTZ)""")
    await q("CREATE INDEX IF NOT EXISTS ws_invites_ws ON ws_invites(workspace_id, created_at DESC)")
    await q("COMMENT ON TABLE ws_invites IS 'GD12.100 invite links: token_hash = sha256(token), the token itself is never stored; accepted anonymously by app/invites.py (system session), RLS_DIRECT for workspace sessions'")

async def down(q):
    await q("DROP TABLE IF EXISTS ws_invites")

async def plan(q):
    return ["CREATE TABLE ws_invites (id, workspace_id -> workspaces ON DELETE CASCADE, token_hash UNIQUE (sha256, never the token), role admin|editor|viewer, email_note, created_by, "
            "created_at, expires_at, max_uses 1-20, used_count <= max_uses, revoked_at) + index (workspace_id, created_at); RLS_DIRECT by admin.rls_init"]
