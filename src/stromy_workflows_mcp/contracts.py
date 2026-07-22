"""Read-only consumer of Stromy's generated hosted workflow contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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


@dataclass(frozen=True)
class Contract:
    workflow: str
    schema: dict[str, Any]
    keys: dict[str, ConfigKey]

    def describe(self, role: CallerRole) -> dict[str, Any]:
        keys = [key for key in self.keys.values() if role is CallerRole.OPERATOR or key.tier != 3]
        return {
            "workflow": self.workflow,
            "description": self.schema.get("description", ""),
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
    return Contract(workflow=workflow, schema=raw, keys=_parse_properties(props))


def list_contracts() -> list[str]:
    return sorted(path.stem for path in contracts_root().glob("*.json"))
