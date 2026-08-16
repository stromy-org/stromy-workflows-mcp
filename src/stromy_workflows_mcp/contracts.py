"""Read-only consumer of Stromy's generated hosted workflow contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: Mirror of ``stromy_byok.models.CredentialId``'s grammar, for the same reason
#: ``entitlements.KNOWN_INPUT_ADAPTERS`` mirrors Stromy's adapter registry: the
#: value travels here inside a generated contract, so the shape is checkable
#: from this side alone. A credential id becomes part of a Key Vault secret
#: name, and Key Vault permits only ``[0-9a-zA-Z-]``.
_CREDENTIAL_ID_RE = re.compile(r"^[0-9a-zA-Z]([0-9a-zA-Z-]*[0-9a-zA-Z])?$")


class CallerRole(StrEnum):
    CLIENT = "client"
    OPERATOR = "operator"


class ContractError(ValueError):
    pass


class ConfigRejected(ValueError):
    def __init__(self, message: str, *, code: str, keys: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.keys = keys or []


@dataclass(frozen=True)
class ConfigKey:
    name: str
    tier: int
    ask: str | None = None
    default: Any = None
    pinned: Any = None
    description: str | None = None


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for name, item in value.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict):
            flattened.update(_flatten(item, path))
        else:
            flattened[path] = item
    return flattened


def _unflatten(value: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for path, item in value.items():
        cursor = nested
        parts = path.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise ContractError(f"contract path collision at {path!r}")
            cursor = child
        cursor[parts[-1]] = item
    return nested


def _parse_properties(properties: dict[str, Any], prefix: str = "") -> dict[str, ConfigKey]:
    keys: dict[str, ConfigKey] = {}
    for name, spec in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        nested = spec.get("properties") if isinstance(spec, dict) else None
        if isinstance(nested, dict):
            keys.update(_parse_properties(nested, path))
            continue
        if not isinstance(spec, dict):
            raise ContractError(f"property {path!r} must be an object")
        tier = spec.get("x-tier")
        if tier not in {1, 2, 3}:
            raise ContractError(f"property {path!r} has invalid x-tier {tier!r}")
        if tier == 1 and not spec.get("x-ask"):
            raise ContractError(f"tier-1 property {path!r} has no x-ask")
        if tier == 3 and "x-pinned" not in spec:
            raise ContractError(f"tier-3 property {path!r} has no x-pinned")
        keys[path] = ConfigKey(
            name=path,
            tier=tier,
            ask=spec.get("x-ask"),
            default=spec.get("default"),
            pinned=spec.get("x-pinned"),
            description=spec.get("description"),
        )
    return keys


# --- Credential requirements (ORG-PLAN-206 C4 item 4) ------------------------
#
# WHAT a run needs to authenticate, as a technical fact of the workflow. It is
# deliberately NOT part of the caller's `config`: the caller never submits,
# overrides or even names a credential. `properties` is the caller's surface;
# this top-level key is the server's.
#
# Requirements are SEMANTIC (a model tier and its capabilities), because the
# concrete provider behind a tier is a deployment-profile fact that changes
# without the workflow changing. `resolved_credentials` is the generated
# concrete projection of those semantics under the deployed profile, and
# `model_registry_digest` is what lets the runner detect that the profile moved
# out from under a synchronized contract (C5's `credential_manifest_drift`).


@dataclass(frozen=True)
class ModelRequirement:
    """One model call this workflow makes, described by tier rather than model."""

    tier: str
    capabilities: tuple[str, ...] = ()
    pin: str | None = None


@dataclass(frozen=True)
class CredentialRequirements:
    """The parsed ``x-credential-requirements`` block, or the undeclared sentinel.

    ``declared`` separates "this contract declares that it needs nothing" from
    "this contract has not been authored against C6 yet". They look identical as
    an empty list and mean opposite things to a client-policy run: the first can
    proceed, the second cannot be run on a client's own keys at all, because the
    server has no statement of what to inject. Collapsing them would turn a
    missing declaration into a silent operator-funded run — the exact failure
    this plane exists to remove.
    """

    declared: bool = False
    models: tuple[ModelRequirement, ...] = ()
    credentials: tuple[str, ...] = ()
    resolved_credentials: tuple[str, ...] = ()
    model_registry_digest: str | None = None

    def describe(self, role: CallerRole) -> dict[str, Any]:
        """What this caller may see of the workflow's credential requirements.

        A client sees the credential IDs they could be asked to register — that
        is actionable and is what the registration surface keys off. The model
        tiers, capability sets and pins stay operator-only for the same reason
        tier-3 config keys do: they describe which provider and profile Stromy
        runs behind the tier, which is a provider-locked commercial fact rather
        than something the client configures.
        """
        payload: dict[str, Any] = {
            "declared": self.declared,
            "credentials": list(self.credentials),
            "resolved_credentials": list(self.resolved_credentials),
        }
        if role is CallerRole.OPERATOR:
            payload["models"] = [
                {
                    "tier": model.tier,
                    "capabilities": list(model.capabilities),
                    **({"pin": model.pin} if model.pin else {}),
                }
                for model in self.models
            ]
            payload["model_registry_digest"] = self.model_registry_digest
        return payload


def _credential_ids(workflow: str, field: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContractError(f"contract {workflow!r} has a non-list {field!r}")
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _CREDENTIAL_ID_RE.fullmatch(value):
            raise ContractError(
                f"contract {workflow!r} declares invalid credential id {value!r} in {field!r}"
            )
        if value in ids:
            raise ContractError(
                f"contract {workflow!r} repeats credential id {value!r} in {field!r}"
            )
        ids.append(value)
    return tuple(ids)


def _parse_models(workflow: str, raw: Any) -> tuple[ModelRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContractError(f"contract {workflow!r} has a non-list 'models'")
    models: list[ModelRequirement] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ContractError(f"contract {workflow!r} has a non-object model requirement")
        tier = entry.get("tier")
        if not isinstance(tier, str) or not tier:
            raise ContractError(f"contract {workflow!r} has a model requirement with no 'tier'")
        capabilities = entry.get("capabilities") or []
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ContractError(
                f"contract {workflow!r} model tier {tier!r} has non-string 'capabilities'"
            )
        pin = entry.get("pin")
        if pin is not None and not isinstance(pin, str):
            raise ContractError(f"contract {workflow!r} model tier {tier!r} has a non-string 'pin'")
        models.append(
            ModelRequirement(tier=tier, capabilities=tuple(capabilities), pin=pin)
        )
    return tuple(models)


def _parse_requirements(workflow: str, raw: Any) -> CredentialRequirements:
    """Parse the top-level ``x-credential-requirements`` block.

    Raises rather than degrading to "no requirements". A malformed block is
    indistinguishable from an absent one once it is swallowed, and "absent"
    is precisely the state a client-policy run must refuse — so a parse that
    fell back to the default would convert a broken generated contract into a
    quiet operator-funded run.
    """
    if raw is None:
        return CredentialRequirements()
    if not isinstance(raw, dict):
        raise ContractError(f"contract {workflow!r} has a non-object 'x-credential-requirements'")
    digest = raw.get("model_registry_digest")
    if digest is not None and not isinstance(digest, str):
        raise ContractError(f"contract {workflow!r} has a non-string 'model_registry_digest'")
    return CredentialRequirements(
        declared=True,
        models=_parse_models(workflow, raw.get("models")),
        credentials=_credential_ids(workflow, "credentials", raw.get("credentials")),
        resolved_credentials=_credential_ids(
            workflow, "resolved_credentials", raw.get("resolved_credentials")
        ),
        model_registry_digest=digest,
    )


@dataclass(frozen=True)
class Contract:
    workflow: str
    schema: dict[str, Any]
    keys: dict[str, ConfigKey]
    requirements: CredentialRequirements = CredentialRequirements()

    def summarize(self, role: CallerRole) -> dict[str, Any]:
        """Enough to CHOOSE a workflow; ``describe`` carries enough to configure one.

        Discovery used to return the full contract for every visible workflow, which
        made ``describe_workflow`` a strict subset of ``list_workflows`` — so a skill
        instructed to call ``describe`` first never had a reason to, and the catalog
        listing grew with the square of the estate.
        """
        return {
            "workflow": self.workflow,
            "description": self.schema.get("description", ""),
            "questions": [
                key.ask
                for key in sorted(self.keys.values(), key=lambda item: item.name)
                if key.tier == 1 and key.ask
            ],
        }

    def describe(self, role: CallerRole) -> dict[str, Any]:
        keys = [key for key in self.keys.values() if role is CallerRole.OPERATOR or key.tier != 3]
        return {
            "workflow": self.workflow,
            "description": self.schema.get("description", ""),
            # Reported beside the config contract, never inside it: these are
            # things the SERVER resolves, not things the caller submits.
            "credential_requirements": self.requirements.describe(role),
            "keys": [
                {
                    "name": key.name,
                    "tier": key.tier,
                    "ask": key.ask,
                    "default": key.default,
                    "description": key.description,
                    **({"pinned": key.pinned} if role is CallerRole.OPERATOR else {}),
                }
                for key in sorted(keys, key=lambda item: (item.tier, item.name))
            ],
        }

    def project(self, effective: dict[str, Any], role: CallerRole) -> dict[str, Any]:
        """Filter a validated config down to what this caller may SEE.

        ``validate`` deliberately returns the FULL effective config, pins included:
        ``start_run`` persists exactly that for the runner, which needs them. But a
        provider-locked key is locked in both directions — ``describe`` already hides
        tier 3 from a client, so echoing the same keys back out of ``validate_config``
        would hand over every budget, model tier, and internal stage toggle the tier
        exists to keep private. Apply this to caller-facing returns ONLY, never to
        what is stored or sent to the runner.
        """
        if role is CallerRole.OPERATOR:
            return effective
        locked = {name for name, key in self.keys.items() if key.tier == 3}
        return _unflatten(
            {
                name: value
                for name, value in _flatten(effective).items()
                if name not in locked
            }
        )

    def validate(self, config: dict[str, Any], role: CallerRole) -> dict[str, Any]:
        errors = sorted(Draft202012Validator(self.schema).iter_errors(config), key=str)
        if errors:
            raise ConfigRejected(errors[0].message, code="schema_invalid")
        flat = _flatten(config)
        unknown = sorted(set(flat) - set(self.keys))
        if unknown:
            raise ConfigRejected(
                f"unknown config key(s): {', '.join(unknown)}",
                code="unknown_key",
                keys=unknown,
            )
        locked = sorted(key for key in flat if self.keys[key].tier == 3)
        if role is CallerRole.CLIENT and locked:
            raise ConfigRejected(
                f"tier-3 key(s) are provider-locked: {', '.join(locked)}",
                code="tier3_forbidden",
                keys=locked,
            )
        effective: dict[str, Any] = {}
        for name, key in self.keys.items():
            if key.tier == 3:
                effective[name] = key.pinned
            elif name in flat:
                effective[name] = flat[name]
            elif key.default is not None:
                effective[name] = key.default
        if role is CallerRole.OPERATOR:
            effective.update({name: value for name, value in flat.items() if name in locked})
        missing = sorted(
            key.name for key in self.keys.values() if key.tier == 1 and key.name not in effective
        )
        if missing:
            raise ConfigRejected(
                f"missing required tier-1 key(s): {', '.join(missing)}",
                code="missing_required",
                keys=missing,
            )
        return _unflatten(effective)


def contracts_root() -> Path:
    return (PROJECT_ROOT / settings.contracts_dir).resolve()


def load_contract(workflow: str) -> Contract:
    path = contracts_root() / f"{workflow}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load contract {workflow!r}: {exc}") from exc
    if raw.get("workflow") != workflow:
        raise ContractError(f"contract file {path} declares {raw.get('workflow')!r}")
    props = raw.get("properties")
    if not isinstance(props, dict):
        raise ContractError(f"contract {workflow!r} has no properties")
    return Contract(
        workflow=workflow,
        schema=raw,
        keys=_parse_properties(props),
        # Parsed off the raw payload explicitly rather than left to be read out
        # of `schema` ad hoc. `sync_contracts.rendered` preserves every
        # top-level key except `x-template-paths`, so the block arrives intact;
        # this is what makes retaining it a tested property of the parser
        # instead of an accident of the copy step.
        requirements=_parse_requirements(workflow, raw.get("x-credential-requirements")),
    )


def list_contracts() -> list[str]:
    return sorted(path.stem for path in contracts_root().glob("*.json"))
