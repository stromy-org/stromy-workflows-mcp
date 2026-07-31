#!/usr/bin/env python3
"""Generate the facade's contract bundle from Stromy's authored contracts."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

WORKFLOWS = ("stakeholder_analysis_workflow", "weekly_intel_workflow")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stromy_workflows_mcp.entitlements import (  # noqa: E402
    KNOWN_ARTIFACT_ADAPTERS,
    KNOWN_INPUT_ADAPTERS,
)


def _registry_names(source_root: Path) -> dict[str, set[str]] | None:
    """Read Stromy's adapter registry keys without importing Stromy.

    Parsed with ``ast`` rather than imported because Stromy is a heavy private
    package this script must not need installed. Returns None when the runner
    source is not present, which is the normal case in this repo's CI.
    """
    module = source_root / "stromy" / "runtime" / "adapters.py"
    if not module.exists():
        return None
    tree = ast.parse(module.read_text())
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in {"INPUT_ADAPTERS", "ARTIFACT_ADAPTERS"}:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        names: set[str] = set()
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
            elif isinstance(key, ast.Name):
                # A sentinel referenced by constant name; resolve it from the
                # module's own top-level assignments.
                for other in ast.walk(tree):
                    if (
                        isinstance(other, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == key.id
                            for t in other.targets
                        )
                        and isinstance(other.value, ast.Constant)
                        and isinstance(other.value.value, str)
                    ):
                        names.add(other.value.value)
        found[node.target.id] = names
    return found or None


def check_adapter_mirror(source_root: Path) -> list[str]:
    """Assert the facade's adapter allowlist matches Stromy's live registry.

    The allowlist in ``entitlements.py`` is a hand-kept mirror (this repo's CI
    cannot reach the private runner). This check is the drift detector, and it
    only runs where both checkouts exist — operator-local and in the org tree.
    """
    registry = _registry_names(source_root)
    if registry is None:
        return []
    drift: list[str] = []
    for label, mirrored, key in (
        ("input", KNOWN_INPUT_ADAPTERS, "INPUT_ADAPTERS"),
        ("artifact", KNOWN_ARTIFACT_ADAPTERS, "ARTIFACT_ADAPTERS"),
    ):
        live = registry.get(key, set())
        if live != set(mirrored):
            drift.append(
                f"{label} adapter mirror drift: facade has "
                f"{sorted(mirrored)}, Stromy registers {sorted(live)}"
            )
    return drift


def rendered(source: Path) -> str:
    payload = json.loads(source.read_text())
    # Template coverage is an authoring/CI ledger in Stromy, not part of the
    # client-facing schema served by this facade.
    payload.pop("x-template-paths", None)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "components" / "resources" / "contracts"
    destination.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for workflow in WORKFLOWS:
        source = args.source_root / "stromy" / "workflows" / workflow / "config" / "contract.json"
        target = destination / f"{workflow}.json"
        expected = rendered(source)
        if args.check:
            if not target.exists() or target.read_text() != expected:
                drift.append(workflow)
        else:
            target.write_text(expected)
            print(f"wrote {target}")
    if drift:
        print("contract bundle drift: " + ", ".join(drift))
        return 1
    mirror_drift = check_adapter_mirror(args.source_root)
    if mirror_drift:
        for line in mirror_drift:
            print(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
