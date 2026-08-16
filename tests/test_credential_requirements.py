"""Top-level ``x-credential-requirements`` retention (ORG-PLAN-206 C4 item 4).

Two properties carry the weight here, and they pull in opposite directions:

1. the block must SURVIVE the parser and reach ``describe_workflow`` — it is the
   server's statement of what a run needs to authenticate; and
2. it must stay entirely OUT of caller ``config`` validation — the caller never
   submits, overrides or names a credential.

A test that only asserted (1) would pass just as happily against a parser that
had quietly merged the block into the config surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stromy_workflows_mcp.contracts import (
    CallerRole,
    ConfigRejected,
    Contract,
    ContractError,
    CredentialRequirements,
    _parse_properties,
    _parse_requirements,
    contracts_root,
    load_contract,
)

_REQUIREMENTS: dict[str, Any] = {
    "models": [
        {"tier": "cheap", "capabilities": ["chat"]},
        {"tier": "smart", "capabilities": ["chat", "reasoning"], "pin": "gpt-5"},
    ],
    "credentials": ["apify-api"],
    "resolved_credentials": ["deepseek-api", "openai-api"],
    "model_registry_digest": "sha256:abc123",
}


def _contract(requirements: Any, *, workflow: str = "wf") -> Contract:
    """A minimal contract carrying one tier-1 key plus the block under test."""
    schema: dict[str, Any] = {
        "workflow": workflow,
        "additionalProperties": False,
        "properties": {"topic": {"type": "string", "x-tier": 1, "x-ask": "Topic?"}},
    }
    return Contract(
        workflow=workflow,
        schema=schema,
        keys=_parse_properties(schema["properties"]),
        requirements=_parse_requirements(workflow, requirements),
    )


# --- The block survives the parser ------------------------------------------


def test_requirements_parse_into_typed_fields() -> None:
    parsed = _parse_requirements("wf", _REQUIREMENTS)
    assert parsed.declared is True
    assert parsed.credentials == ("apify-api",)
    assert parsed.resolved_credentials == ("deepseek-api", "openai-api")
    assert parsed.model_registry_digest == "sha256:abc123"
    assert [model.tier for model in parsed.models] == ["cheap", "smart"]
    assert parsed.models[1].capabilities == ("chat", "reasoning")
    assert parsed.models[1].pin == "gpt-5"
    assert parsed.models[0].pin is None


def test_describe_surfaces_requirements_to_a_client() -> None:
    described = _contract(_REQUIREMENTS).describe(CallerRole.CLIENT)
    surfaced = described["credential_requirements"]
    assert surfaced["declared"] is True
    assert surfaced["credentials"] == ["apify-api"]
    assert surfaced["resolved_credentials"] == ["deepseek-api", "openai-api"]


def test_describe_withholds_model_internals_from_a_client() -> None:
    """Tier and pin say which provider/profile runs behind a tier.

    That is the same provider-locked commercial fact tier-3 config keys exist to
    withhold, so surfacing requirements must not become a side door onto it.
    """
    surfaced = _contract(_REQUIREMENTS).describe(CallerRole.CLIENT)["credential_requirements"]
    assert "models" not in surfaced
    assert "model_registry_digest" not in surfaced
    assert "gpt-5" not in json.dumps(surfaced)


def test_describe_gives_the_operator_the_full_block() -> None:
    surfaced = _contract(_REQUIREMENTS).describe(CallerRole.OPERATOR)["credential_requirements"]
    assert surfaced["model_registry_digest"] == "sha256:abc123"
    assert [model["tier"] for model in surfaced["models"]] == ["cheap", "smart"]
    assert surfaced["models"][1]["pin"] == "gpt-5"
    assert "pin" not in surfaced["models"][0]


# --- ...and stays out of the caller's config surface ------------------------


def test_requirements_never_become_a_config_key() -> None:
    contract = _contract(_REQUIREMENTS)
    assert "x-credential-requirements" not in contract.keys
    assert not any("credential" in name for name in contract.keys)


def test_a_caller_cannot_submit_requirements_as_config() -> None:
    """The whole point: credentials are server-derived, never caller-supplied."""
    contract = _contract(_REQUIREMENTS)
    with pytest.raises(ConfigRejected) as exc:
        contract.validate(
            {"topic": "x", "x-credential-requirements": {"resolved_credentials": ["free-api"]}},
            CallerRole.CLIENT,
        )
    assert exc.value.code in {"schema_invalid", "unknown_key"}


def test_requirements_never_reach_the_effective_config() -> None:
    """``validate`` returns what the RUNNER is handed; the block must not ride along."""
    effective = _contract(_REQUIREMENTS).validate({"topic": "x"}, CallerRole.OPERATOR)
    assert effective == {"topic": "x"}


# --- Undeclared is not the same as "needs nothing" ---------------------------


def test_absent_block_parses_as_undeclared_not_as_empty_requirements() -> None:
    """A client-policy run can proceed on "needs nothing" and must refuse "unknown".

    Collapsing the two would turn a contract nobody has authored requirements
    for into a silent operator-funded run — the failure this plane exists to
    remove.
    """
    absent = _parse_requirements("wf", None)
    empty = _parse_requirements("wf", {})
    assert absent == CredentialRequirements()
    assert absent.declared is False
    assert empty.declared is True
    assert absent.resolved_credentials == empty.resolved_credentials == ()
    assert absent != empty


def test_undeclared_is_visible_in_describe() -> None:
    surfaced = _contract(None).describe(CallerRole.CLIENT)["credential_requirements"]
    assert surfaced["declared"] is False


# --- Malformed fails loudly rather than degrading to "no requirements" -------


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ("not-an-object", "non-object 'x-credential-requirements'"),
        ({"credentials": "apify-api"}, "non-list 'credentials'"),
        ({"credentials": ["not a valid id"]}, "invalid credential id"),
        ({"credentials": ["-leading"]}, "invalid credential id"),
        ({"resolved_credentials": ["openai-api", "openai-api"]}, "repeats credential id"),
        ({"models": {"tier": "cheap"}}, "non-list 'models'"),
        ({"models": ["cheap"]}, "non-object model requirement"),
        ({"models": [{"capabilities": ["chat"]}]}, "no 'tier'"),
        ({"models": [{"tier": "cheap", "capabilities": "chat"}]}, "non-string 'capabilities'"),
        ({"models": [{"tier": "cheap", "pin": 5}]}, "non-string 'pin'"),
        ({"model_registry_digest": 5}, "non-string 'model_registry_digest'"),
    ],
)
def test_malformed_requirements_raise(block: Any, match: str) -> None:
    """The mutation that matters: degrading to the default would read as undeclared.

    ``declared=False`` is a meaningful state that blocks a client-policy run, so
    a swallowed parse error would present a BROKEN contract as an unauthored
    one — the same wrong answer, reached without anyone noticing.
    """
    with pytest.raises(ContractError, match=match):
        _parse_requirements("wf", block)


def test_credential_id_grammar_matches_key_vault_secret_naming() -> None:
    """Ids become part of a Key Vault secret name: ``[0-9a-zA-Z-]`` only."""
    ok = _parse_requirements("wf", {"credentials": ["openai-api", "a", "a1-b2"]})
    assert ok.credentials == ("openai-api", "a", "a1-b2")
    for bad in ("openai_api", "openai.api", "openai api", "trailing-", "", "OPENAI/API"):
        with pytest.raises(ContractError, match="invalid credential id"):
            _parse_requirements("wf", {"credentials": [bad]})


# --- The shipped contracts ---------------------------------------------------


def test_shipped_contracts_load_and_report_requirement_state() -> None:
    """C6 authors the real blocks; until then every contract is undeclared.

    Pinned so the C6 authoring pass has to come back through this test rather
    than landing a block the parser silently ignores.
    """
    for path in sorted(Path(contracts_root()).glob("*.json")):
        contract = load_contract(path.stem)
        described = contract.describe(CallerRole.OPERATOR)["credential_requirements"]
        assert described["declared"] is contract.requirements.declared
        assert contract.requirements.declared is False, (
            f"{path.stem} now declares credential requirements — update this test "
            "and confirm the resolved list matches the deployed model profile"
        )
