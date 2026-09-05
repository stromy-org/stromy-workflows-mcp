"""Example tools — replace with your own.

`echo` is the minimal smoke tool. `example_list` is the shape to copy for any
tool that returns a LIST, a TABLE, or LONG TEXT: it declares an output schema
and it budgets its complete result server-side. See
`infra-docs/ai/mcp-response-budgeting.md` (stromy-org) for the standard, and
`references/component-shapes.md` in this template for the short version.
"""

from typing import Annotated, Any

from fastmcp.tools import tool
from pydantic import BaseModel, Field

from stromy_workflows_mcp.response_budget import fit_json_result


@tool
def echo(message: str) -> str:
    """Echo a message back to the caller."""
    return message


class ExampleItem(BaseModel):
    """One row. Keep rows narrow: every field is paid for on every row."""

    id: str
    label: str


class ResponseBudget(BaseModel):
    """How much of the client's context this response consumed."""

    limit_content_chars: int
    content_chars: int
    utf8_bytes: int
    kept_count: int
    dropped_count: int
    truncated: bool


class ExampleListResult(BaseModel):
    """Declared output schema — the machine-checkable half of the contract.

    FastMCP mirrors a structured return into BOTH `structuredContent` and a
    serialized JSON `TextContent`, so the schema describes exactly what the
    client is billed for. Declaring it is what lets a test assert the two halves
    agree instead of eyeballing a dict.
    """

    results: list[ExampleItem]
    returned_count: int
    total_count: int | None
    warnings: list[str]
    metadata: dict[str, Any]


@tool
def example_list(
    query: str,
    max_results: Annotated[int, Field(ge=1, le=500)] = 50,
) -> ExampleListResult:
    """Search example items. Replace with your real tool.

    Note what this example does NOT do: it does not treat `max_results` as the
    thing that keeps the response safe. The row cap is a UX bound — it says how
    many rows the caller wants, not how many characters the client can accept.
    The size guarantee comes from `fit_json_result`, which serializes the final
    payload and fits it to the response budget. A truncated response says so,
    keeps its continuation handle, and names the NARROWING move.
    """
    matched = [
        ExampleItem(id=f"{query}-{n}", label=f"Example item {n}") for n in range(max_results)
    ]

    def build_payload(prefix, budget_meta, warning):
        # Called repeatedly by the fitter, so keep it pure. Fixed metadata,
        # pre-existing warnings and coverage all belong here — they are part of
        # what the client pays for, so they must be inside the measured payload.
        return {
            "results": [item.model_dump() for item in prefix],
            "returned_count": len(prefix),
            "total_count": len(matched),
            "warnings": [warning] if warning else [],
            "metadata": {
                "query": {"filters": {"query": query}, "limit": max_results},
                "coverage": {
                    "status": "partial" if len(prefix) < len(matched) else "complete",
                    "returned": len(prefix),
                    "next_offset": len(prefix) if len(prefix) < len(matched) else None,
                },
                "response_budget": budget_meta,
            },
        }

    fitted = fit_json_result(
        matched,
        build_payload=build_payload,
        narrowing_hint=(
            "Narrow the query or request a later page with the offset in "
            "metadata.coverage.next_offset."
        ),
    )
    return ExampleListResult.model_validate(fitted.payload)
