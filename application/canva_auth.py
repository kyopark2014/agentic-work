"""Per-user Canva (remote MCP) OAuth tokens.

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
start = oauth.start
finish = oauth.finish
get_access_token = oauth.get_access_token
status_for_user = oauth.status_for_user
clear_token = oauth.clear_token
mcp_selected = oauth.mcp_selected
is_token_fresh = oauth.is_token_fresh
