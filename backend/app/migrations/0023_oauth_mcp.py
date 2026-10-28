"""GĐ11.26 MCP OAuth: probe, PRM, registration. `oauth_providers` gets: registration ('manual' | 'dcr' | 'cimd' | 'preregistered' = how the client id was obtained: typed by an admin /
dynamic client registration RFC 7591 / client-id metadata document / an existing provider adopted), registration_client_uri (RFC 7592, informational), registration_token_enc (the
registration access token, SEALED, write-only) and redirect_uri_registered (the redirect address the authorization server was told about).
Existing rows keep behaving exactly as before (registration defaults to 'manual'; the other three columns are NULL).
down(): drops the four columns (the providers themselves stay)."""
VERSION = "0023"
NAME = "oauth_mcp"

_PROV = ["registration TEXT NOT NULL DEFAULT 'manual'", "registration_client_uri TEXT", "registration_token_enc TEXT", "redirect_uri_registered TEXT"]
_CHECK = ("oauth_providers_registration_chk", "registration IN ('manual','dcr','cimd','preregistered')")

async def up(q):
    await q("ALTER TABLE oauth_providers " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _PROV))
    await q(f"ALTER TABLE oauth_providers DROP CONSTRAINT IF EXISTS {_CHECK[0]}")
    await q(f"ALTER TABLE oauth_providers ADD CONSTRAINT {_CHECK[0]} CHECK ({_CHECK[1]})")

async def down(q):
    await q(f"ALTER TABLE oauth_providers DROP CONSTRAINT IF EXISTS {_CHECK[0]}")
    await q("ALTER TABLE oauth_providers " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _PROV))

async def plan(q):
    return ["oauth_providers: ADD registration (default manual; CHECK manual|dcr|cimd|preregistered), registration_client_uri, registration_token_enc (sealed), redirect_uri_registered; existing rows unchanged"]
