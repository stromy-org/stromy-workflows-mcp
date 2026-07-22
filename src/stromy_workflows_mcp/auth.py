"""Azure / Microsoft Entra ID OAuth provider construction.

Gated by ``settings.oauth_enable``. When disabled (the default), this returns
``None`` and FastMCP runs unauthenticated. When enabled, the required Azure
App Registration credentials must be present in the environment or the server
fails fast at startup.

Durable sessions
----------------
``AzureProvider`` (built on FastMCP's ``OAuthProxy``) keeps auth state that must
survive container restarts, or clients re-authenticate on every cold start /
scale-to-zero event. FastMCP derives the three keys it needs — the JWT signing
key, the storage encryption key, and the storage path fingerprint —
*deterministically from the stable upstream OAuth client secret*, so they are
already stable across restarts with **no app config**. The ONLY ephemeral piece
is the store **directory contents** on local container disk.

We make that durable in **infrastructure, not app code**: an Azure Files share
mounted at FastMCP's default home path (``/home/appuser/.local/share/fastmcp``)
persists the store across restarts / scale-to-zero (ORG-PLAN-073). No Redis, no
extra dependencies, no extra env vars. Enable it per server with
``oauth_sessions = "files"`` in the terraform fragment (see ``azure_aca/README.md``).
Locally, the default in-memory / on-disk store needs no configuration.

See README "OAuth (Microsoft Entra ID)" for Azure App Registration setup.
"""

from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger(__name__)


def build_auth_provider():
    """Return an ``AzureProvider`` when OAuth is enabled, else ``None``."""
    if not settings.oauth_enable:
        return None

    from fastmcp.server.auth.providers.azure import AzureProvider

    scopes = [s.strip() for s in settings.oauth_required_scopes.split(",") if s.strip()]

    missing = [
        name
        for name, value in (
            ("OAUTH_CLIENT_ID", settings.oauth_client_id),
            ("OAUTH_CLIENT_SECRET", settings.oauth_client_secret),
            ("OAUTH_TENANT_ID", settings.oauth_tenant_id),
            ("OAUTH_BASE_URL", settings.oauth_base_url),
        )
        if not value
    ]
    if not scopes:
        missing.append("OAUTH_REQUIRED_SCOPES")
    if missing:
        raise RuntimeError(
            "OAUTH_ENABLE=true but required Azure settings are missing: "
            + ", ".join(missing)
            + ". See README 'OAuth (Microsoft Entra ID)'."
        )

    if settings.fastmcp_transport == "stdio":
        logger.warning(
            "OAUTH_ENABLE=true with FASTMCP_TRANSPORT=stdio — "
            "FastMCP auth is not applied to stdio transport."
        )

    # Keys are derived from the (stable) client secret; durability of the store
    # dir is provided by the Azure Files home mount, not by app config. So we
    # pass no jwt_signing_key / client_storage here (ORG-PLAN-073).
    return AzureProvider(
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        tenant_id=settings.oauth_tenant_id,
        base_url=settings.oauth_base_url,
        required_scopes=scopes,
    )
