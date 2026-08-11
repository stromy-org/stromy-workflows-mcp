#!/usr/bin/env python3
"""Gate: every hosted workflow carries an explicit client-entitlement decision.

Runs in CI (`.github/workflows/ci.yml`) and is deliberately repo-local — unlike
`sync_contracts.py --check`, which needs a private `../../Stromy` checkout that
this public repository's CI cannot reach, and therefore never actually executes
there.

The point is that a new workflow cannot land without someone deciding who may run
it. `clients: []` (operator-only) is a perfectly good answer; silence is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stromy_workflows_mcp.contracts import (  # noqa: E402
    ContractError,
    list_contracts,
    load_contract,
)
from stromy_workflows_mcp.entitlements import (  # noqa: E402
    EntitlementError,
    adapter_problems,
    entitlements_path,
    load_entitlements,
)


def main() -> int:
    try:
        table = load_entitlements()
    except EntitlementError as exc:
        print(f"FAIL  {exc}")
        return 1

    contracts = set(list_contracts())
    entitled = set(table)
    registry = entitlements_path().name
    problems: list[str] = [
        f"contract {workflow!r} has no entry in {registry}. "
        'Add one — use "clients": [] if it is operator-only.'
        for workflow in sorted(contracts - entitled)
    ]
    problems.extend(
        f"entitlement entry {workflow!r} names a workflow with no contract in "
        "components/resources/contracts/. Remove it, or sync the contract."
        for workflow in sorted(entitled - contracts)
    )

    # Every workflow declares an executable data plane; a client-entitled one
    # must declare a REAL one (ORG-PLAN-164 WS0).
    adapters: dict[str, tuple[str | None, str | None]] = {}
    for workflow in sorted(contracts & entitled):
        try:
            contract = load_contract(workflow)
        except ContractError as exc:
            problems.append(str(exc))
            continue
        problems.extend(
            adapter_problems(
                workflow, contract.schema, has_clients=bool(table[workflow])
            )
        )
        adapters[workflow] = (
            contract.schema.get("x-input-adapter"),
            contract.schema.get("x-artifact-adapter"),
        )

    if problems:
        print(f"FAIL  workflow entitlements ({len(problems)} problem(s))")
        for problem in problems:
            print(f"      - {problem}")
        return 1

    print(f"PASS  workflow entitlements ({len(contracts)} workflow(s))")
    for workflow in sorted(contracts):
        clients = sorted(table[workflow])
        inbound, outbound = adapters.get(workflow, (None, None))
        print(
            f"      {workflow}: {', '.join(clients) if clients else '(operator-only)'} "
            f"[in={inbound} out={outbound}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
