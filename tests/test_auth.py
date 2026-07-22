"""Unit tests for the OAuth provider builder."""

from unittest.mock import patch

import pytest

from stromy_workflows_mcp import auth as auth_module


def test_returns_none_when_disabled():
    with patch.object(auth_module.settings, "oauth_enable", False):
        assert auth_module.build_auth_provider() is None


def test_raises_when_enabled_without_config():
    with patch.multiple(
        auth_module.settings,
        oauth_enable=True,
        oauth_client_id="",
        oauth_client_secret="",
        oauth_tenant_id="",
        oauth_base_url="",
        oauth_required_scopes="",
    ):
        with pytest.raises(RuntimeError, match="OAUTH_ENABLE=true"):
            auth_module.build_auth_provider()


def test_builds_provider_when_fully_configured():
    with patch.multiple(
        auth_module.settings,
        oauth_enable=True,
        oauth_client_id="cid",
        oauth_client_secret="secret",  # noqa: S106
        oauth_tenant_id="tid",
        oauth_base_url="http://localhost:8000",
        oauth_required_scopes="mcp.access,mcp.read",
        fastmcp_transport="http",
    ):
        provider = auth_module.build_auth_provider()
        assert provider is not None
