"""The OAuth login contract a real MCP client depends on.

WHY THIS FILE EXISTS
--------------------
Before it, every auth assertion in this repo was about *provider construction*
(``test_auth.py``). Nothing asserted the one property every client actually
depends on — that a client can complete a login — so a major bump of the auth
library was invisible to every gate by construction. On 2026-09-09 that gap let
fastmcp 3.4.7 -> 4.0.2 land across nine MCPs on one night with green suites,
clean trial merges and ``/health`` 200 everywhere, and lock every affected MCP
client out of ``asset-broker-mcp`` for six days (asset-broker-mcp#26/#39). This
server took the same bump on the same night and carries the same defect. It was
not missed; it was unobservable.

These tests run fully offline against the real Starlette app (ASGI transport,
no network, no Entra, no browser) and take milliseconds. They assert the
**client-visible** half of the flow — the part ``/health`` can never speak to.

WHAT THEY GUARD
---------------
1. Discovery is well-formed and internally consistent (RFC 9728 + RFC 8414).
2. Dynamic client registration accepts an RFC 8252 loopback redirect URI.
3. The authorization endpoint redirects **back to the client's own
   redirect_uri** and the client-facing response carries **exactly** the
   parameters RFC 6749 defines — nothing more. This is the regression guard:
   any newly-introduced client-visible parameter (fastmcp 4's RFC 9207 ``iss``
   being the worked example) fails here, loudly, in CI, instead of silently in
   a partner's terminal.
4. The advertised capabilities match what the server actually does. A server
   that advertises ``authorization_response_iss_parameter_supported`` obliges
   every compliant client to *reject* a response without ``iss`` — so
   advertising it is a promise about client behaviour, not a server detail.

A green run here does NOT prove a real login works end to end against Entra. It
proves the PROTOCOL CONTRACT. The deployment is proved by stromy-org's
``scripts/audit-mcp-login.py``, which runs the same client-visible assertions
against the live revision (hourly as ``com.stromy.mcp-login-canary``, nightly as
arbiter class L). Both are needed: this one blocks the bad version from merging,
that one catches a bad version that reached production some other way.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from stromy_workflows_mcp import auth as auth_module

# An RFC 8252 §7.3 loopback redirect with an ephemeral port — the shape every
# CLI and desktop MCP client uses, and the shape that broke in 2026-09.
LOOPBACK_REDIRECT = "http://127.0.0.1:49813/callback/abc123"
BASE_URL = "https://workflows.test.invalid"

# RFC 6749 §4.1.2 / §4.1.2.1 — the complete set of parameters an authorization
# server may put on a client-facing authorization response. `iss` (RFC 9207) is
# deliberately NOT here: see the module docstring.
ALLOWED_SUCCESS_PARAMS = {"code", "state"}
ALLOWED_ERROR_PARAMS = {"error", "error_description", "error_uri", "state"}


@pytest.fixture
def oauth_app() -> Any:
    """The real server's ASGI app with OAuth switched on against stub credentials.

    No network: nothing here reaches Entra. The provider is constructed exactly
    as production constructs it, so the routes, the metadata documents and the
    redirect construction are the production ones.
    """
    with patch.multiple(
        auth_module.settings,
        oauth_enable=True,
        oauth_client_id="00000000-0000-0000-0000-000000000000",
        oauth_client_secret="stub-secret",  # noqa: S106
        oauth_tenant_id="11111111-1111-1111-1111-111111111111",
        oauth_base_url=BASE_URL,
        oauth_required_scopes="mcp.access",
        fastmcp_transport="http",
    ):
        from fastmcp import FastMCP

        server = FastMCP("stromy-workflows-oauth-contract", auth=auth_module.build_auth_provider())
        yield server.http_app()


@pytest.fixture
async def http(oauth_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=oauth_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        # http_app() carries a lifespan; the metadata routes do not need it, but
        # entering it keeps the app in its production shape.
        yield c


async def _register(
    http: httpx.AsyncClient, redirect_uri: str = LOOPBACK_REDIRECT
) -> dict[str, Any]:
    r = await http.post(
        "/register",
        json={
            "client_name": "login-contract-test",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp.access",
        },
    )
    assert r.status_code == 201, f"DCR rejected a loopback client: {r.status_code} {r.text}"
    return r.json()


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------- discovery


async def test_protected_resource_metadata_is_wellformed(http: httpx.AsyncClient) -> None:
    """RFC 9728: the client's entry point. Without this, discovery never starts."""
    r = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    prm = r.json()
    assert prm["resource"].endswith("/mcp")
    assert prm["authorization_servers"], "no authorization server advertised"


async def test_authorization_server_metadata_is_wellformed(http: httpx.AsyncClient) -> None:
    """RFC 8414: every endpoint a client needs must be present and PKCE-capable."""
    r = await http.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    asm = r.json()
    for field in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert asm.get(field), f"AS metadata missing {field}"
    assert "S256" in asm.get("code_challenge_methods_supported", []), "PKCE S256 not offered"
    assert "code" in asm.get("response_types_supported", [])


async def test_issuer_matches_the_advertised_authorization_server(http: httpx.AsyncClient) -> None:
    """RFC 8414 §3.3 — `issuer` must equal the URL the client discovered.

    A mismatch here (a stray trailing slash is the classic) makes a strict client
    abandon the flow with no server-side error at all.
    """
    prm = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    asm = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert asm["issuer"].rstrip("/") == prm["authorization_servers"][0].rstrip("/")


async def test_does_not_advertise_rfc9207_iss(http: httpx.AsyncClient) -> None:
    """Advertising `iss` is a promise about CLIENT behaviour, so it is a breaking change.

    RFC 9207 §2.4 and the MCP spec both require a client to REJECT an
    authorization response that omits `iss` once the server advertises support.
    Any client whose loopback callback relay drops unknown query parameters
    therefore stops being able to log in the moment this flips true — and it
    fails *before* the token request, so the server sees a clean 302 and then
    silence. That is the 2026-09-09 regression.

    Flip this deliberately, having verified a real login for every client class
    we support — never as a side effect of a dependency bump.
    """
    asm = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert not asm.get("authorization_response_iss_parameter_supported"), (
        "The server now advertises RFC 9207 `iss`. Every compliant client must "
        "reject an `iss`-less response, so any client with a lossy callback relay "
        "is locked out with no error. See the module docstring."
    )


# ------------------------------------------------- dynamic client registration


async def test_registers_a_loopback_client(http: httpx.AsyncClient) -> None:
    """RFC 8252 §7.3 loopback with an ephemeral port — the CLI/desktop client shape."""
    reg = await _register(http)
    assert reg["client_id"]
    assert LOOPBACK_REDIRECT in reg["redirect_uris"]


async def test_registration_preserves_the_redirect_uri_verbatim(http: httpx.AsyncClient) -> None:
    """A rewritten redirect_uri lands on a path the client is not listening on."""
    reg = await _register(http)
    assert reg["redirect_uris"] == [LOOPBACK_REDIRECT]


# ------------------------------------------------ the client-facing response


async def test_error_response_goes_back_to_the_client_with_only_rfc6749_params(
    http: httpx.AsyncClient,
) -> None:
    """THE regression guard, and it needs no IdP.

    An unsupported `response_type` is the one client-facing authorization
    response an offline test can provoke: the server must redirect to the
    client's own redirect_uri carrying an OAuth error. That redirect is built by
    the same code path as the success redirect, so the parameter set it carries
    is the parameter set a real login carries.
    """
    reg = await _register(http)
    _, challenge = _pkce()
    r = await http.get(
        "/authorize",
        params={
            "response_type": "token",  # unsupported -> error redirect to the client
            "client_id": reg["client_id"],
            "redirect_uri": LOOPBACK_REDIRECT,
            "scope": "mcp.access",
            "state": "contract-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code in (302, 307), (
        f"expected a redirect back to the client, got {r.status_code}"
    )
    location = r.headers["location"]
    parsed = urlparse(location)

    assert location.startswith(LOOPBACK_REDIRECT.split("?")[0]), (
        f"authorization response did not go back to the client's redirect_uri: {location}"
    )

    params = parse_qs(parsed.query)
    assert params.get("state") == ["contract-state"], "state not echoed — a client would abort"
    assert params.get("error"), "no OAuth error code on an error response"

    unexpected = set(params) - ALLOWED_ERROR_PARAMS
    assert not unexpected, (
        f"the authorization response carries parameter(s) {sorted(unexpected)} that RFC 6749 "
        "does not define. A client whose callback relay drops unknown parameters, or which "
        "validates them strictly, will abandon the login before requesting a token — with no "
        "server-side error to show for it. Add it here only with a verified real login "
        "for every client class we support."
    )


async def test_authorization_request_starts_the_flow(http: httpx.AsyncClient) -> None:
    """A valid request must move the user agent on (consent or the upstream IdP).

    Asserted separately from the error path because a server can regress into
    400-ing a perfectly good loopback request (fastmcp #3674 did exactly that to
    CIMD clients with dynamic ports).
    """
    reg = await _register(http)
    _, challenge = _pkce()
    r = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": LOOPBACK_REDIRECT,
            "scope": "mcp.access",
            "state": "contract-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code in (302, 307), (
        f"a valid loopback authorization request was not accepted: {r.status_code} {r.text[:300]}"
    )
    location = r.headers["location"]
    assert not location.startswith(LOOPBACK_REDIRECT), (
        f"the server bounced a valid request straight back to the client: {location}"
    )


# ----------------------------------------------------------- the 401 handshake


async def test_unauthenticated_mcp_call_points_at_the_metadata(http: httpx.AsyncClient) -> None:
    """RFC 9728 §5.1 — the 401 must carry `WWW-Authenticate` with `resource_metadata`.

    This is how a client discovers it needs to log in at all. Without it the
    client has a 401 and nowhere to go, which presents to the user as "the
    server is broken", not "you need to sign in".
    """
    r = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert "resource_metadata" in www, (
        f"401 does not point at the protected-resource metadata: {www!r}"
    )
