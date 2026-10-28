# Tool connections (per-user OAuth 2.0)

Agents call third-party APIs **as the user who started the run**, using the `local:oauth_request(provider, path_or_url, method, json_body)` tool.

## How it works
- Flow: Authorization Code + PKCE (S256), a one-time `state` (10 min, bound to the user), and a callback at `https://<your host>/oauth/callback`. The callback requires a signed-in user.
- Storage: access and refresh tokens and client secrets are Fernet-encrypted with `AOS_OAUTH_KEY` in `/opt/langchain/.env`. To rotate, move the old key to `AOS_OAUTH_KEY_OLD`.
- Refresh: happens automatically when a token is within 60 s of expiry, or manually with **Refresh**. If the refresh token is rejected, the status becomes `needs_reconnect`.
- Revoke: **Disconnect** calls the provider's RFC 7009 revocation endpoint (when one is set), then deletes the local tokens.
- Safety: the tool can only reach URLs under the provider's **API base**. Every connect, refresh, tool call and revoke is recorded in the audit log (Settings → Tool connections).
- To enable it for an agent, add `local:oauth_request` to the agent's tools.

## Adding a real provider (admin)
Go to Settings → Tool connections → **Add provider**, pick a preset, then paste the client ID and secret. The redirect URI to register is shown on that page.

| Provider | Where to create the app | Notes |
|---|---|---|
| GitHub | github.com/settings/developers → OAuth Apps → New | Callback URL = redirect URI. Classic OAuth Apps issue non-expiring tokens and have no revoke URL (users revoke on GitHub). PKCE is supported. |
| Google | console.cloud.google.com → APIs & Services → Credentials → OAuth client ID (Web) | Add the redirect URI. Configure the consent screen and enable the APIs you need. The preset sends `access_type=offline&prompt=consent` so Google returns a refresh token. |
| Slack | api.slack.com/apps → Create app → OAuth & Permissions | Add the redirect URL. The preset requests user scopes via `user_scope`. Slack returns the user token under `authed_user`, so use a bot-token style scope or adjust if needed. |
| Microsoft 365 | entra.microsoft.com → App registrations → New (Web) | Add the redirect URI and create a client secret. Include `offline_access` in the scopes to get refresh tokens. |

The built-in mock server (`AOS_MOCK_OAUTH=1`, client `agentos-mock-client` / `mock-secret`) exercises the whole flow. Set `AOS_MOCK_OAUTH=0` in production when you no longer need it.

## Testing
`python3 tests/roadmap_test.py oauth` runs 14 checks: PKCE, bad state, bad code, encryption at rest, one-time state, refresh, auto-refresh, an agent tool call, revoke, and audit.

## Engine options (GĐ11.25)
- **Network policy**: every call the server makes to a provider (token, revoke, userinfo, the `oauth_request` tool) goes through `netguard` purpose `oauth`: https only, private / metadata addresses refused, redirects not followed. A provider on a local address (tests, an internal IdP) works only when the operator lists it in `AOS_EGRESS_ALLOW`.
- **Public address**: set `AOS_PUBLIC_BASE=https://<host>` (no path). `X-Forwarded-Host` / `-Proto` are believed only from a peer listed in `AOS_TRUSTED_PROXIES` (IPs / CIDRs). With neither, the redirect uses the request's own `Host`; providers in `tunnel` mode then refuse to connect (409).
- **Redirect mode** per provider: `web` (`<public base>/oauth/callback`), `tunnel` (same, but the address is registered at the provider/MCP server: when `AOS_PUBLIC_BASE` changes the connections become `needs_reconnect` (`redirect_changed`) and an admin presses **Re-register**; nothing is re-registered automatically), `loopback_paste` (Google / Microsoft without a domain: redirect `http://127.0.0.1:<AOS_LOOPBACK_PORT, default 8765>/oauth/callback`; after signing in the browser cannot open that page, the user pastes its address into the dialog = `POST /api/oauth/paste-callback`). `default_redirect_uri` (https, or http on 127.0.0.1 / [::1]) overrides the mode.
- **Grant type**: `authorization_code` (default) or `client_credentials` (the token is fetched when the provider is saved, a failure saves nothing; the credential belongs to the workspace; "refresh" fetches a new one).
- **Token endpoint**: `token_auth` body | post | basic | none (public client), `token_params` (plain extra fields), `token_headers` (extra headers, sealed, write-only), `resource` (RFC 8707), `issuer` + `iss_supported` (RFC 9207: a wrong or missing `iss` is refused), `allowed_scopes` (ceiling: a member can narrow the scopes, never widen them).
- **Catalog**: `app/oauth_catalog.json` (25 services) -> `GET /api/connections/catalog`; `POST /api/oauth/providers/from-catalog {catalog_id, client_id, client_secret}`. The endpoints were written from the vendors' public documentation and have not been tried against the real services yet.
- Tests: `tests/test_gd11_oauth.py` (against the fake servers of `tests/fakes/fake_mcp_oauth.py`; its HTTP part starts its own server on port 8023).

