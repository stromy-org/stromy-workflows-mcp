"""Registration-link minting and the /keys route mount (ORG-PLAN-206 C4).

The link is a *capability*: whoever holds it may bind a credential for one
subject. So the tests that matter are the ones proving the four gates cannot be
walked around — visibility, acting-for, entitlement, requirement — and that the
grant's scope comes from the server rather than from anything a caller sends.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from stromy_workflows_mcp import credentials, server, service
from stromy_workflows_mcp.contracts import (
    Contract,
    CredentialRequirements,
    _parse_properties,
)
from stromy_workflows_mcp.scoping import CallerScope

WORKFLOW = "stakeholder_analysis_workflow"
OPERATOR = CallerScope(frozenset(), unrestricted=True)
STROMY = CallerScope(frozenset({"stromy"}))
OUTSIDER = CallerScope(frozenset({"someone-else"}))


def _contract_with(requirements: CredentialRequirements) -> Contract:
    schema: dict[str, Any] = {
        "workflow": WORKFLOW,
        "properties": {"topic": {"type": "string", "x-tier": 1, "x-ask": "Topic?"}},
    }
    return Contract(
        workflow=WORKFLOW,
        schema=schema,
        keys=_parse_properties(schema["properties"]),
        requirements=requirements,
    )


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the workflow to a declared requirement set.

    The shipped contracts are still undeclared (C6 authors them), so the happy
    path has to be staged rather than read off disk.
    """
    contract = _contract_with(
        CredentialRequirements(
            declared=True,
            credentials=("apify-api",),
            resolved_credentials=("openai-api", "deepseek-api"),
        )
    )
    monkeypatch.setattr(service, "load_contract", lambda name: contract)


def _mint(scope: CallerScope, credential_id: str = "openai-api", **kwargs: Any) -> dict[str, Any]:
    return service.create_credential_registration_link(
        WORKFLOW, credential_id, kwargs.pop("client_context", {"client_slug": "stromy"}), scope,
        **kwargs,
    )


# --- The happy path ----------------------------------------------------------


def test_mints_a_single_use_link_carrying_the_token(declared: None) -> None:
    minted = _mint(STROMY)
    assert minted["client_slug"] == "stromy"
    assert minted["credential_id"] == "openai-api"
    assert minted["provider"] == "OpenAI"
    assert "/keys?token=" in minted["registration_url"]
    assert minted["expires_in_seconds"] > 0

    token = minted["registration_url"].split("token=", 1)[1]
    grant = credentials.GRANTS.peek(token)
    assert grant is not None
    # Everything that scopes the write is bound at mint time, so the page can
    # accept a token and a key and nothing else.
    assert grant.credential_id == "openai-api"
    assert grant.service == credentials.SERVICE
    assert grant.workflow == WORKFLOW
    assert grant.subject.kind.value == "client-slug"


def test_two_links_are_distinct_capabilities(declared: None) -> None:
    first = _mint(STROMY)["registration_url"]
    assert first != _mint(STROMY)["registration_url"]


def test_a_non_model_declared_credential_can_be_registered(declared: None) -> None:
    assert _mint(STROMY, "apify-api")["credential_id"] == "apify-api"


def test_disconnect_action_binds_into_the_grant(declared: None) -> None:
    minted = _mint(STROMY, action="disconnect")
    token = minted["registration_url"].split("token=", 1)[1]
    grant = credentials.GRANTS.peek(token)
    assert grant is not None and grant.action.value == "disconnect"


def test_unknown_action_is_rejected(declared: None) -> None:
    with pytest.raises(ValueError, match="unknown action"):
        _mint(STROMY, action="delete-everything")


# --- Gate 1: visibility ------------------------------------------------------


def test_unentitled_caller_cannot_tell_the_workflow_exists(declared: None) -> None:
    """Same contract `entitlements` keeps: unknown and unentitled read alike.

    Otherwise link-minting becomes a catalog oracle — a caller could enumerate
    the estate by diffing "unknown workflow" against "not entitled".
    """
    with pytest.raises(Exception, match="unknown workflow"):
        _mint(OUTSIDER, client_context={"client_slug": "someone-else"})


# --- Gate 2: acting-for ------------------------------------------------------


def test_operator_must_name_a_client_and_never_implies_one(declared: None) -> None:
    with pytest.raises(PermissionError, match="client_slug is required"):
        _mint(OPERATOR, client_context={})


def test_operator_may_act_for_a_named_client(declared: None) -> None:
    assert _mint(OPERATOR)["client_slug"] == "stromy"


def test_client_cannot_mint_for_a_slug_it_does_not_hold(declared: None) -> None:
    with pytest.raises(PermissionError, match="not authorized for client"):
        _mint(STROMY, client_context={"client_slug": "dukestrategies"})


# --- Gate 3: entitlement of the RESOLVED owner -------------------------------


def test_entitlement_is_checked_against_the_resolved_owner(declared: None) -> None:
    """Holding two roles, entitled via one, must not register a key for the other.

    The union check that governs visibility cannot catch this — it has no owner
    to check against — so the key would be spent on runs owned by a client that
    was never entitled to the workflow.
    """
    both = CallerScope(frozenset({"stromy", "unentitled-co"}))
    with pytest.raises(PermissionError, match="not entitled to workflow"):
        _mint(both, client_context={"client_slug": "unentitled-co"})


# --- Gate 4: requirement -----------------------------------------------------


def test_a_credential_the_workflow_does_not_declare_is_refused(declared: None) -> None:
    """Otherwise any catalogue id could be registered under cover of any workflow."""
    with pytest.raises(ValueError, match="does not use credential"):
        _mint(STROMY, "hunter-api")


def test_undeclared_requirements_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No declaration means "is this credential one of them?" has no answer.

    Minting anyway would let a client register a key against a workflow that
    never uses it — which then reads as connected, forever.
    """
    monkeypatch.setattr(
        service, "load_contract", lambda name: _contract_with(CredentialRequirements())
    )
    with pytest.raises(ValueError, match="declares no credential requirements"):
        _mint(STROMY)


def test_requirement_is_checked_before_the_catalogue(declared: None) -> None:
    """Ordering keeps the catalogue un-enumerable.

    A caller who has not already named a credential the workflow declares must
    not be able to learn which ids the catalogue knows, so the contract check
    has to answer first for BOTH an unknown id and a known-but-unused one.
    """
    with pytest.raises(ValueError, match="does not use credential"):
        _mint(STROMY, "not-a-real-credential")


# --- The catalogue's own invariants -----------------------------------------


def test_every_declared_credential_is_caller_funded() -> None:
    """An OPERATOR spec here would be a hole in the client-mode scrub.

    The scrub list derives from `caller_funded_env_aliases()`, so a credential
    declared operator-owned would stay live in the environment of a client-mode
    run — the silent operator-spend path this plane exists to close.
    """
    assert credentials.CATALOGUE.caller_funded() == tuple(credentials.CATALOGUE)


def test_scrub_list_covers_every_alias_including_the_duplicates() -> None:
    aliases = set(credentials.CATALOGUE.caller_funded_env_aliases())
    # Both Apify names are real; declaring one would leave a live caller-funded
    # key in a client-mode run's environment.
    assert {"APIFY_API_TOKEN", "APIFY_TOKEN"} <= aliases
    assert {"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY"} <= aliases
    assert {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "HUNTER_API_KEY"} <= aliases


def test_every_spec_declares_a_probe_and_a_signup_url() -> None:
    """A spec with no probe stores unverified; one with no signup url strands a user."""
    for spec in credentials.CATALOGUE:
        assert spec.probe is not None, f"{spec.credential_id} has no validator"
        assert spec.signup_url, f"{spec.credential_id} has no signup url"


def test_gemini_treats_400_as_a_definitive_rejection() -> None:
    """Measured behaviour: Gemini answers a bad key with 400, not 401.

    Under the default set that would classify as merely unverified, so a broken
    key would be stored and only fail later, inside a run.
    """
    probe = credentials.CATALOGUE.get("google-genai").probe
    assert probe is not None
    assert 400 in probe.invalid_statuses


def test_no_probe_carries_the_key_in_a_query_string() -> None:
    for spec in credentials.CATALOGUE:
        assert spec.probe is not None
        assert "?" not in spec.probe.url, f"{spec.credential_id} probe URL has a query string"
        assert spec.probe.url.startswith("https://")


# --- The mounted routes ------------------------------------------------------


def test_keys_routes_are_mounted_and_hardened() -> None:
    with TestClient(server.mcp.http_app()) as client:
        response = client.get("/keys?token=not-a-real-grant")
    # An invalid grant renders the generic page, never a 404 from an unmounted
    # route — so this asserts the mount as well as the denial.
    assert response.status_code == 404
    assert "invalid" in response.text.lower()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_a_minted_link_opens_its_registration_page(declared: None) -> None:
    token = _mint(STROMY)["registration_url"].split("token=", 1)[1]
    with TestClient(server.mcp.http_app()) as client:
        response = client.get(f"/keys?token={token}")
    assert response.status_code == 200
    assert "OpenAI" in response.text
    # GET peeks; it must not spend the grant, or a reload would burn the link.
    assert credentials.GRANTS.peek(token) is not None


def test_the_registration_tool_is_exposed() -> None:
    import asyncio

    from fastmcp import Client

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    assert "create_credential_registration_link" in asyncio.run(names())
