"""GĐ11.27 runtime auth per caller + pre-run connection gate. `auth_sessions` = one "please connect <connection>" request handed to a person by a paused run
(tool answer `auth_required` or the pre-run gate): auth_id (random, 192 bit, the only handle), the person (user_id) and workspace it is for, the owner the account will be stored
under, the connection slug, the scope asked for, status pending | completed | failed | expired, return_to (a local path), expires (10 min). The browser opens
GET /api/agent-auth/sessions/{auth_id}/start, which creates the normal one-time OAuth state (oauth_states.auth_id links the two) and redirects to the provider; the callback marks
the session completed / failed; the chat card long-polls GET /api/agent-auth/sessions/{auth_id}. The table has a workspace_id and is in admin.RLS_DIRECT (registered by agent_identity.init).
The oauth_states table is created here when missing (a fresh DB runs migrations before oauth_tools.init), in the shape oauth_tools.DDL creates.
down(): drops the table and the column (pending sign-ins simply stop working)."""
VERSION = "0024"
NAME = "auth_sessions"

_STATES = """CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY, provider_id INT, user_id INT, verifier_enc TEXT, next TEXT,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL)"""
_SESSIONS = """CREATE TABLE IF NOT EXISTS auth_sessions(auth_id TEXT PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, user_id INT NOT NULL,
  owner_type TEXT NOT NULL DEFAULT 'user' CHECK (owner_type IN ('user','agent','workspace')), owner_id INT NOT NULL DEFAULT 0, slug TEXT NOT NULL, scope TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed','expired')), return_to TEXT,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ)"""

async def up(q):
    await q(_STATES)
    await q("ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS auth_id TEXT")
    await q(_SESSIONS)
    await q("CREATE INDEX IF NOT EXISTS auth_sessions_lookup ON auth_sessions(workspace_id, user_id, slug, status)")

async def down(q):
    await q("DROP TABLE IF EXISTS auth_sessions")
    await q("ALTER TABLE oauth_states DROP COLUMN IF EXISTS auth_id")

async def plan(q):
    return ["auth_sessions: CREATE (auth_id PK, workspace_id, user_id, owner, slug, scope, status pending|completed|failed|expired, return_to, expires) + index",
            "oauth_states: ADD auth_id (links a one-time state to its session)"]
