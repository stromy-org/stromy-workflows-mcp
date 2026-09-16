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
    DEFAULT_CREDENTIAL_FUNDING,
    DEFAULT_CREDENTIAL_POLICY,
    POLICY_CLIENT,
    POLICY_OPERATOR,
    EntitlementError,
    FundingDecision,
    _parse,
    credential_funding,
    credential_policy,
    entitled_clients,
    entitlements_path,
)

#: A declared set to resolve synthetic decisions against. Funding is meaningless
#: until it meets the credentials a workflow actually spends.
DECLARED = ("openai-api", "serper-api")


def _table(clients: object, workflow: str = "wf") -> dict[str, dict[str, FundingDecision]]:
    return _parse({"workflows": {workflow: {"clients": clients}}})


def _funders(clients: object, slug: str = "alpha", declared: object = DECLARED) -> dict[str, str]:
    """Effective funder per credential, dropping the decided flag."""
    resolved = _table(clients)["wf"][slug].resolve(declared)
    return {cid: entry.funded_by for cid, entry in resolved.items()}


# --- v2: the object form carries the commercial fact -------------------------


def test_v2_object_form_reads_explicit_policies() -> None:
    """The scalar shorthand still means "every declared credential on this policy"."""
    clients = {
        "alpha": {"credential_policy": POLICY_CLIENT},
        "beta": {"credential_policy": POLICY_OPERATOR},
    }
    assert _funders(clients, "alpha") == dict.fromkeys(DECLARED, POLICY_CLIENT)
    assert _funders(clients, "beta") == dict.fromkeys(DECLARED, POLICY_OPERATOR)


def test_v2_entry_without_a_policy_defaults_to_operator() -> None:
    assert _funders({"alpha": {}}) == dict.fromkeys(DECLARED, DEFAULT_CREDENTIAL_FUNDING)


def test_v2_null_client_config_defaults_to_operator() -> None:
    """`"alpha": null` is a grant with nothing said about billing, not a broken entry."""
    assert _funders({"alpha": None}) == dict.fromkeys(DECLARED, DEFAULT_CREDENTIAL_FUNDING)


# --- Per-credential funding (ORG-PLAN-300) -----------------------------------


def test_funding_map_decides_each_credential_independently() -> None:
    """The shape the whole plan exists for: the client pays for the model, we
    pay for the flat-rate search subscription."""
    clients = {"alpha": {"funding": {"openai-api": "client", "serper-api": "operator"}}}
    assert _funders(clients) == {"openai-api": POLICY_CLIENT, "serper-api": POLICY_OPERATOR}


def test_a_credential_absent_from_the_map_defaults_and_says_so() -> None:
    """The default must not block a first deployment — and must not hide either.

    `decided` is the whole reason a default is acceptable here: lending our key
    is the right starting state, but a funding answer nobody chose has to be
    distinguishable from one somebody did.
    """
    resolved = _table({"alpha": {"funding": {"openai-api": "client"}}})["wf"]["alpha"].resolve(
        DECLARED
    )
    assert (resolved["openai-api"].funded_by, resolved["openai-api"].decided) == (
        POLICY_CLIENT,
        True,
    )
    assert (resolved["serper-api"].funded_by, resolved["serper-api"].decided) == (
        DEFAULT_CREDENTIAL_FUNDING,
        False,
    )


def test_an_explicit_operator_decision_is_not_a_default() -> None:
    """NEGATIVE CONTROL for `decided`: if it were computed from the VALUE rather
    than from presence, an explicit "we pay" would be indistinguishable from
    nobody having decided — and the operator report of undecided credentials
    would quietly list every deliberate choice."""
    resolved = _table({"alpha": {"funding": {"serper-api": "operator"}}})["wf"]["alpha"].resolve(
        DECLARED
    )
    assert (resolved["serper-api"].funded_by, resolved["serper-api"].decided) == (
        POLICY_OPERATOR,
        True,
    )
    assert (resolved["openai-api"].funded_by, resolved["openai-api"].decided) == (
        POLICY_OPERATOR,
        False,
    )


def test_declaring_both_shapes_is_refused() -> None:
    """Two statements of one answer that can disagree. Resolving by precedence
    would make the billing answer depend on which field a reader consulted."""
    with pytest.raises(EntitlementError, match="BOTH"):
        _table({"alpha": {"credential_policy": POLICY_CLIENT, "funding": {"openai-api": "client"}}})


def test_an_unknown_funding_value_is_refused() -> None:
    """Same mutation as the scalar's typo guard, at per-credential granularity."""
    with pytest.raises(EntitlementError, match="serper-api"):
        _table({"alpha": {"funding": {"serper-api": "operatr"}}})


def test_a_non_object_funding_is_refused() -> None:
    with pytest.raises(EntitlementError, match="non-object 'funding'"):
        _table({"alpha": {"funding": ["openai-api"]}})


def test_funding_keys_outside_the_declared_set_are_reported() -> None:
    """A decision written against the wrong workflow looks exactly like one that
    took effect. `check_entitlements.py` turns this into a CI failure."""
    decision = _table({"alpha": {"funding": {"openai-api": "client", "ghost-api": "client"}}})[
        "wf"
    ]["alpha"]
    assert decision.undeclared_keys(DECLARED) == ("ghost-api",)
    assert decision.undeclared_keys(("openai-api", "ghost-api")) == ()


def test_coarse_policy_is_client_when_any_credential_is(monkeypatch) -> None:
    """One client-funded credential makes this a client-funded RUN.

    The surfaces that ask a yes/no question — does this need a scrub, does the
    client owe a registration — must not answer "operator" for a mixed pair just
    because most of it is operator-funded.
    """
    clients = {"alpha": {"funding": {"openai-api": "client", "serper-api": "operator"}}}
    monkeypatch.setattr(
        "stromy_workflows_mcp.entitlements.load_entitlements", lambda: _table(clients)
    )
    assert credential_policy("wf", "alpha", DECLARED) == POLICY_CLIENT
    # NEGATIVE CONTROL: all-operator must NOT read as client.
    operator_only = {"alpha": {"funding": {"openai-api": "operator"}}}
    monkeypatch.setattr(
        "stromy_workflows_mcp.entitlements.load_entitlements", lambda: _table(operator_only)
    )
    assert credential_policy("wf", "alpha", DECLARED) == POLICY_OPERATOR


# --- v1 compatibility: the window where registry and code disagree -----------


def test_v1_list_form_still_parses_as_every_slug_on_the_default() -> None:
    """A v1 file must not fail closed against a v2 parser.

    Registry and code deploy independently. If this raised, the whole client
    catalogue would deny at once during the rollout window — an outage caused by
    a migration that changes nobody's billing.
    """
    assert _funders(["alpha", "beta"], "alpha") == dict.fromkeys(
        DECLARED, DEFAULT_CREDENTIAL_FUNDING
    )
    assert _funders(["alpha", "beta"], "beta") == dict.fromkeys(
        DECLARED, DEFAULT_CREDENTIAL_FUNDING
    )


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
        lambda: {"wf": {"alpha": FundingDecision(shorthand=POLICY_CLIENT)}},
    )
    assert credential_policy("wf", "alpha", DECLARED) == POLICY_CLIENT
    assert credential_policy("wf", "nobody", DECLARED) == DEFAULT_CREDENTIAL_POLICY
    assert credential_policy("missing", "alpha", DECLARED) == DEFAULT_CREDENTIAL_POLICY
    # And the per-credential surface agrees with the coarse one.
    assert all(
        entry.funded_by == POLICY_CLIENT
        for entry in credential_funding("wf", "alpha", DECLARED).values()
    )


def test_entitled_clients_reads_the_object_form(monkeypatch) -> None:
    monkeypatch.setattr(
        "stromy_workflows_mcp.entitlements.load_entitlements",
        lambda: {
            "wf": {
                "alpha": FundingDecision(shorthand=POLICY_CLIENT),
                "beta": FundingDecision(shorthand=POLICY_OPERATOR),
            }
        },
    )
    assert entitled_clients("wf") == frozenset({"alpha", "beta"})


# --- The shipped registry ----------------------------------------------------


#: The only slug allowed to ship on `client`. Stromy is its own client — the
#: self-client dogfooding surface — so moving it costs no external party
#: anything, which is exactly why the C6 end-to-end proof runs there.
SELF_CLIENT = "stromy"


def _declared_for(workflow: str) -> tuple[str, ...]:
    """Every credential the shipped contract says this workflow spends."""
    from stromy_workflows_mcp.contracts import load_contract

    requirements = load_contract(workflow).requirements
    return tuple(requirements.all_declared)


def test_shipped_registry_bills_no_external_client_without_a_decision() -> None:
    """No *paying* client's billing moves as a side effect of a code change.

    C4's original form asserted every shipped grant was `operator`, on the
    argument that flipping one is a separate commercial act. C6 then made
    exactly that act — for the self-client, and for one reason: the credential
    plane cannot be proven end to end without a pair on `client`, and proving it
    on `stromy` keeps a live client out of the experiment. Flipping
    `dukestrategies` instead would have made every Duke run demand a key Duke has
    never registered, failing them all closed at stage `credentials` — a real
    outage staged as a test.

    So the guard is narrowed rather than dropped, and it is narrowed to the one
    thing worth guarding: a slug that is somebody else's company must not arrive
    on client-funded billing through a merge. Deleting this test would have been
    the easy way to make the flip green, and would have retired the only
    mechanism that makes the next flip deliberate.
    """
    raw = json.loads(Path(entitlements_path()).read_text())
    assert raw["version"] == 2

    table = _parse(raw)
    assert table, "the shipped registry parsed to nothing"
    for workflow, clients in table.items():
        declared = _declared_for(workflow)
        for slug, decision in clients.items():
            if slug == SELF_CLIENT:
                continue
            billed = sorted(
                cid
                for cid, entry in decision.resolve(declared).items()
                if entry.funded_by == POLICY_CLIENT
            )
            assert not billed, (
                f"{workflow}/{slug} ships billing {billed} to the client. Moving "
                "an external client onto client-funded billing is a commercial "
                "act taken with that client, not a code change — if this is "
                "deliberate, say so here and in the registry's note."
            )


def test_the_self_client_is_the_one_pair_proving_the_client_path() -> None:
    """The flip above is an allowance, not a wildcard — assert it was USED.

    Without this, `stromy` could silently drift back to `operator` and the
    narrowed guard would still pass, leaving the whole client-funded path with
    no shipped instance and nothing exercising it end to end.
    """
    table = _parse(json.loads(Path(entitlements_path()).read_text()))
    on_client = {
        (workflow, slug)
        for workflow, clients in table.items()
        for slug, decision in clients.items()
        if any(
            entry.funded_by == POLICY_CLIENT
            for entry in decision.resolve(_declared_for(workflow)).values()
        )
    }
    assert on_client == {("stakeholder_analysis_workflow", SELF_CLIENT)}


def test_the_self_client_still_lets_the_operator_fund_the_subscriptions() -> None:
    """The mixed case must exist in the SHIPPED file, not only in unit fixtures.

    Without a real pair spending both wallets in one run, nothing exercises the
    scrub carve-out end to end and `scrub_except` is dead code in production.
    """
    table = _parse(json.loads(Path(entitlements_path()).read_text()))
    decision = table["stakeholder_analysis_workflow"][SELF_CLIENT]
    resolved = decision.resolve(_declared_for("stakeholder_analysis_workflow"))
    funders = {entry.funded_by for entry in resolved.values()}
    assert funders == {POLICY_CLIENT, POLICY_OPERATOR}, (
        "the self-client pair no longer spends both wallets, so no shipped run "
        f"exercises mixed funding (funders={sorted(funders)})"
    )
    assert all(entry.decided for entry in resolved.values()), (
        "every credential the proof pair spends must be a decision, not a default"
    )


def test_shipped_registry_uses_the_object_form() -> None:
    """Guards the file itself, not just the parser's tolerance of the old shape."""
    raw = json.loads(Path(entitlements_path()).read_text())
    for workflow, entry in raw["workflows"].items():
        assert isinstance(entry["clients"], dict), (
            f"{workflow} still uses the v1 list form; the parser accepts it for "
            "the rollout window, but the shipped file should be v2"
        )
