"""Per-user Canva (remote MCP) OAuth tokens.

**DISABLED** in production UI/config. Kept for a possible future re-enable after
Canva allows non-loopback redirect URIs (partner allowlist). See canva-mcp.md.

Constraints that forced disable:
* Canva DCR clients accept only loopback (``localhost`` / ``127.0.0.1``) redirect URIs.
* Hosted deployments therefore cannot complete OAuth automatically; users must paste
  the failed ``http://localhost:53682/canva/callback?...`` URL back into the app.
* That paste-back UX is unsuitable for a deployed product, so Canva MCP is off.

Canva's hosted MCP (``https://mcp.canva.com/mcp``) uses OAuth 2.0 authorization code
+ PKCE with dynamic client registration (public client). Scopes are tiered:

* read  - browse/read designs, folders, brand templates, comments, assets
* write - additionally create/edit designs, folders, comments, assets
          (requested on demand from the MCP drawer)

The flow itself lives in ``mcp_oauth``.
"""

from __future__ import annotations

try:
    from application.mcp_oauth import McpOAuth, OAuthProvider
except ImportError:  # flat layout
    from mcp_oauth import McpOAuth, OAuthProvider  # type: ignore

# Off until Canva allows https redirect URIs for our domain (no localhost paste-back).
DISABLED = True
DISABLED_REASON = (
    "Canva OAuth only allows localhost redirect URIs for DCR clients; "
    "deployed UX requires paste-back and is disabled."
)

CANVA_MCP_URL = "https://mcp.canva.com/mcp"
CANVA_ISSUER = "https://mcp.canva.com"

READ_SCOPES = (
    "profile:read",
    "design:meta:read",
    "design:content:read",
    "folder:read",
    "brandtemplate:meta:read",
    "brandtemplate:content:read",
    "comment:read",
    "asset:read",
    "brandkit:read",
)
WRITE_SCOPES = READ_SCOPES + (
    "design:content:write",
    "folder:write",
    "brandtemplate:content:write",
    "comment:write",
    "asset:write",
)

PROVIDER = OAuthProvider(
    name="canva",
    mcp_url=CANVA_MCP_URL,
    issuer=CANVA_ISSUER,
    metadata_url=f"{CANVA_ISSUER}/.well-known/oauth-authorization-server",
    client_name="agentic-work",
    token_file="canva_oauth.json",
    state_dir="_canva_oauth",
    read_scope=" ".join(READ_SCOPES),
    write_scope=" ".join(WRITE_SCOPES),
    write_marker="design:content:write",
    selected_names=frozenset({"canva"}),
    # Canva's authorize endpoint rejects every non-loopback redirect URI for dynamically
    # registered clients (verified: example.com and our own https origin both fail with
    # "Invalid redirect URI", localhost/127.0.0.1 pass). Hosted deployments therefore use
    # this loopback URI and the paste-back flow; local runs (loopback Host) use the
    # normal automatic callback.
    loopback_only=True,
    manual_redirect_uri="http://localhost:53682/canva/callback",
)
oauth = McpOAuth(PROVIDER)

token_path = oauth.token_path
finish = oauth.finish
get_access_token = oauth.get_access_token
clear_token = oauth.clear_token
is_token_fresh = oauth.is_token_fresh


def start(user_id: str, redirect_uri: str, *, write: bool = False) -> str:
    if DISABLED:
        raise ValueError(f"Canva MCP is disabled: {DISABLED_REASON}")
    return oauth.start(user_id, redirect_uri, write=write)


def status_for_user(user_id: str | None) -> dict:
    if DISABLED:
        return {"connected": False, "fresh": False, "expires_at": None, "write": False}
    return oauth.status_for_user(user_id)


def mcp_selected(mcp_servers: list | None) -> bool:
    if DISABLED:
        return False
    return oauth.mcp_selected(mcp_servers)
