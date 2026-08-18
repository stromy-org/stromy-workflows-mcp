"""Per-client credential policy in the entitlement registry (ORG-PLAN-206 C4).

The property under test is a BILLING boundary, so the interesting cases are the
ones where a wrong answer costs money silently: a typo'd policy that reads as
`operator`, or a v1 registry meeting a v2 parser and denying everyone at once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stromy_workflows_mcp.entitlements import (
    CREDENTIAL_POLICIES,
    DEFAULT_CREDENTIAL_POLICY,
    POLICY_CLIENT,
    POLICY_OPERATOR,
    EntitlementError,
    _parse,
    credential_policy,
    entitled_clients,
    entitlements_path,
)


def _table(clients: object, workflow: str = "wf") -> dict[str, dict[str, str]]:
    return _parse({"workflows": {workflow: {"clients": clients}}})


# --- v2: the object form carries the commercial fact -------------------------


def test_v2_object_form_reads_explicit_policies() -> None:
    table = _table(
        {
            "alpha": {"credential_policy": POLICY_CLIENT},
            "beta": {"credential_policy": POLICY_OPERATOR},
        }
    )
    assert table["wf"] == {"alpha": POLICY_CLIENT, "beta": POLICY_OPERATOR}


def test_v2_entry_without_a_policy_defaults_to_operator() -> None:
    assert _table({"alpha": {}})["wf"] == {"alpha": DEFAULT_CREDENTIAL_POLICY}


def test_v2_null_client_config_defaults_to_operator() -> None:
    """`"alpha": null` is a grant with nothing said about billing, not a broken entry."""
    assert _table({"alpha": None})["wf"] == {"alpha": DEFAULT_CREDENTIAL_POLICY}


# --- v1 compatibility: the window where registry and code disagree -----------


def test_v1_list_form_still_parses_as_every_slug_on_the_default() -> None:
    """A v1 file must not fail closed against a v2 parser.

    Registry and code deploy independently. If this raised, the whole client
    catalogue would deny at once during the rollout window — an outage caused by
    a migration that changes nobody's billing.
    """
    assert _table(["alpha", "beta"])["wf"] == {
        "alpha": DEFAULT_CREDENTIAL_POLICY,
        "beta": DEFAULT_CREDENTIAL_POLICY,
    }


def test_v1_and_v2_agree_on_who_is_entitled() -> None:
    """The grant set is identical across shapes; only the billing fact is new."""
    assert set(_table(["alpha"])["wf"]) == set(_table({"alpha": {}})["wf"])


# --- Fail closed, never silently cheaper -------------------------------------


def test_unknown_policy_is_an_error_not_a_silent_operator_default() -> None:
    """The mutation that matters: a typo must not bill Stromy for a client run.

    If this ever degraded to `operator`, `credential_policy: "cient"` would read
    as "Stromy pays" on an entry edited specifically to make the client pay, and
    nothing would ever surface it.
    """
    with pytest.raises(EntitlementError, match="unknown credential_policy"):
        _table({"alpha": {"credential_policy": "cient"}})


def test_unknown_policy_error_names_the_valid_values() -> None:
    with pytest.raises(EntitlementError) as exc:
        _table({"alpha": {"credential_policy": "nonsense"}})
    for value in CREDENTIAL_POLICIES:
        assert value in str(exc.value)


def test_invalid_slug_is_rejected_in_the_object_form_too() -> None:
    """v2 must not become a hole in the slug grammar v1 enforced."""
    with pytest.raises(EntitlementError, match="invalid slug"):
        _table({"Not A Slug": {"credential_policy": POLICY_OPERATOR}})


def test_non_object_client_config_is_rejected() -> None:
    with pytest.raises(EntitlementError, match="must be an object or null"):
        _table({"alpha": "operator"})


def test_scalar_clients_value_is_rejected() -> None:
    with pytest.raises(EntitlementError, match="non-list/object"):
        _table("alpha")


# --- Lookup surface ----------------------------------------------------------


def test_credential_policy_defaults_for_an_unknown_pairing(monkeypatch) -> None:
    """Entitlement is decided by the require_* gates, not by this lookup.

    Answering `operator` for an unentitled caller is safe because they never reach
    a policy question; raising here would make the billing answer depend on which
    of two checks ran first.
    """
    monkeypatch.setattr(
        "stromy_workflows_mcp.entitlements.load_entitlements",
        lambda: {"wf": {"alpha": POLICY_CLIENT}},
    )
    assert credential_policy("wf", "alpha") == POLICY_CLIENT
    assert credential_policy("wf", "nobody") == DEFAULT_CREDENTIAL_POLICY
    assert credential_policy("missing", "alpha") == DEFAULT_CREDENTIAL_POLICY


def test_entitled_clients_reads_the_object_form(monkeypatch) -> None:
    monkeypatch.setattr(
        "stromy_workflows_mcp.entitlements.load_entitlements",
        lambda: {"wf": {"alpha": POLICY_CLIENT, "beta": POLICY_OPERATOR}},
    )
    assert entitled_clients("wf") == frozenset({"alpha", "beta"})


# --- The shipped registry ----------------------------------------------------


def test_shipped_registry_is_v2_and_bills_nobody_new() -> None:
    """C4 must change no client's billing on its own.

    The migration's whole safety argument is that every existing grant lands on
    `operator`; flipping one to `client` is a separate, deliberate commercial act.
    """
    raw = json.loads(Path(entitlements_path()).read_text())
    assert raw["version"] == 2

    table = _parse(raw)
    assert table, "the shipped registry parsed to nothing"
    for workflow, clients in table.items():
        for slug, policy in clients.items():
            assert policy == POLICY_OPERATOR, (
                f"{workflow}/{slug} ships on {policy!r}; C4 must migrate every "
                "existing grant to 'operator'"
            )


def test_shipped_registry_uses_the_object_form() -> None:
    """Guards the file itself, not just the parser's tolerance of the old shape."""
    raw = json.loads(Path(entitlements_path()).read_text())
    for workflow, entry in raw["workflows"].items():
        assert isinstance(entry["clients"], dict), (
            f"{workflow} still uses the v1 list form; the parser accepts it for "
            "the rollout window, but the shipped file should be v2"
        )
