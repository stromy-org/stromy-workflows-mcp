#!/usr/bin/env python3
"""Generate the facade's contract bundle from Stromy's authored contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

WORKFLOWS = ("stakeholder_analysis_workflow", "weekly_intel_workflow")


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
