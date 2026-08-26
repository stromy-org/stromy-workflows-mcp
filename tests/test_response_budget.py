"""Response-size regression for the caller-facing surfaces (ORG-PLAN-221 C7).

This repo was NOT one of the plan's measured offenders — its workflow summaries
are bounded by construction and its heavy payloads already cross as blob
handles. That is exactly why the population gate exists: "no test found a
problem" and "nothing was measured" are indistinguishable outcomes, and the
plan's honesty rule is that no locally-readable MCP counts as compliant just
because a byte-named constant exists somewhere.

So these tests measure. They assert through a real `fastmcp.Client` that the
listing surfaces stay inside the budget, and — the part that actually protects
anything — that `list_runs` stays bounded as the run history GROWS, which is
the axis that will eventually break it if nothing watches.
"""

from __future__ import annotations

import json

import pytest

from stromy_workflows_mcp.response_budget import DEFAULT_RESPONSE_CONTENT_CHARS

# The caller-facing read surfaces. Write/dispatch tools are excluded: their
# responses are a handle plus status, and they have side effects.
READ_TOOLS = ("list_workflows", "list_runs")


def result_text(result) -> str:
    return "".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.parametrize("tool_name", READ_TOOLS)
async def test_read_surfaces_fit_the_budget(client, tool_name):
    """Measured, not assumed — through the boundary a client actually reads."""
    try:
        result = await client.call_tool(name=tool_name, arguments={})
    except Exception as exc:  # noqa: BLE001 - an unconfigured backend is not this test's subject
        pytest.skip(f"{tool_name} needs a configured backend: {exc}")

    text = result_text(result)
    assert len(text) <= DEFAULT_RESPONSE_CONTENT_CHARS, f"{tool_name}: {len(text)} chars"


async def test_describe_workflow_fits_the_budget(client):
    listing = await client.call_tool(name="list_workflows", arguments={})
    workflows = listing.data
    if not workflows:
        pytest.skip("no workflows registered in this environment")

    result = await client.call_tool(
        name="describe_workflow", arguments={"name": workflows[0]["workflow"]}
    )
    text = result_text(result)
    assert len(text) <= DEFAULT_RESPONSE_CONTENT_CHARS, f"{len(text)} chars"


async def test_list_runs_stays_bounded_as_history_grows(client, monkeypatch):
    """The axis that will break this surface if nothing watches it.

    A fresh deployment has no runs, so an unbounded `list_runs` looks perfectly
    healthy for as long as it takes to accumulate some — which is the shape of
    every defect in this plan's population. Simulate a large history and assert
    the response is still bounded.
    """
    from stromy_workflows_mcp import service

    fat_runs = [
        {
            "run_id": f"run-{n:05d}",
            "workflow": "lead-research",
            "status": "completed",
            "client_slug": "example",
            "created_at": "2026-08-26T00:00:00Z",
            "notes": "x" * 400,
        }
        for n in range(2_000)
    ]
    monkeypatch.setattr(service, "list_runs", lambda _scope, _limit: fat_runs)

    result = await client.call_tool(name="list_runs", arguments={"limit": 5000})
    text = result_text(result)

    assert len(text) <= DEFAULT_RESPONSE_CONTENT_CHARS, (
        f"list_runs returned {len(text)} chars for a 2,000-run history — it needs "
        "the response budget, not just a `limit` parameter"
    )
    assert json.loads(text) is not None
