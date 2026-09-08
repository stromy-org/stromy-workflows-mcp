<<<<<<< before updating
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
=======
"""Chrome-regression tests for server-side response budgeting (ORG-PLAN-221).

CHROME — do not delete. These protect the contract that keeps tool results
inside a client's context window: the budget is the COMPLETE MCP-visible text,
measured through the real FastMCP boundary, and truncation is loud, resumable,
and never advises raising a cap. `copier update` keeps this file current.

The FastMCP-boundary tests are the point. A unit test that stops at
`len(json.dumps(envelope))` is measuring a string the client never receives —
it omits the runtime's own serialization, and that gap is exactly where the
2026-08-25 oversize incident lived.
"""
>>>>>>> after updating

import json

import pytest

<<<<<<< before updating
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
=======
from stromy_workflows_mcp.response_budget import (
    DEFAULT_RESPONSE_CONTENT_CHARS,
    MAX_RESPONSE_CONTENT_CHARS,
    ResponseBudgetError,
    assert_narrowing_grammar,
    canonical_json,
    excerpt_text,
    fit_json_result,
)

HINT = "Narrow the query or request a later page with metadata.coverage.next_offset."


def simple_builder(fixed_metadata=None):
    def build(prefix, budget_meta, warning):
        return {
            "results": list(prefix),
            "returned_count": len(prefix),
            "warnings": [warning] if warning else [],
            "metadata": {**(fixed_metadata or {}), "response_budget": budget_meta},
        }

    return build


# -- the measurement contract -------------------------------------------------


def test_fitted_text_is_exactly_the_serialized_payload():
    """`serialized_text` and `payload` must describe the same bytes.

    They diverge the moment someone measures once and then patches the size
    field afterwards — and a client that reads TextContent would then see
    different content from one that reads structuredContent.
    """
    items = [{"i": n, "pad": "x" * 200} for n in range(300)]
    fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
    assert fitted.serialized_text == canonical_json(fitted.payload)
    assert json.loads(fitted.serialized_text) == fitted.payload
    assert fitted.content_chars == len(fitted.serialized_text)
    assert fitted.utf8_bytes == len(fitted.serialized_text.encode("utf-8"))


def test_declared_size_never_understates_actual_size():
    """The self-reported budget block is an upper bound, never optimistic."""
    for count in range(0, 400, 37):
        items = [{"i": n, "pad": "y" * 97} for n in range(count)]
        fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
        declared = fitted.payload["metadata"]["response_budget"]
        assert declared["content_chars"] >= fitted.content_chars
        assert declared["utf8_bytes"] >= fitted.utf8_bytes


def test_untruncated_result_keeps_every_item_and_says_so():
    items = [{"i": n} for n in range(10)]
    fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
    assert fitted.kept_count == 10
    assert fitted.dropped_count == 0
    assert fitted.truncated is False
    assert fitted.payload["warnings"] == []
    assert fitted.payload["metadata"]["response_budget"]["truncated"] is False


def test_truncation_drops_a_suffix_so_an_offset_can_resume():
    items = [{"i": n, "pad": "z" * 500} for n in range(400)]
    fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
    assert fitted.truncated
    assert fitted.payload["results"] == items[: fitted.kept_count]
    assert fitted.kept_count + fitted.dropped_count == len(items)


def test_exact_boundary_is_respected_not_approximated():
    """Sweep a range of budgets; every fitted result must be at or under it."""
    items = [{"i": n, "pad": "b" * 64} for n in range(200)]
    for limit in (1_000, 2_048, 5_000, 12_345, DEFAULT_RESPONSE_CONTENT_CHARS):
        fitted = fit_json_result(
            items,
            build_payload=simple_builder(),
            narrowing_hint=HINT,
            max_content_chars=limit,
        )
        assert fitted.content_chars <= limit, (limit, fitted.content_chars)


def test_fit_is_maximal_one_more_item_would_overflow():
    """Fitting must find the LARGEST prefix, not a lazily conservative one.

    Rows here are a fixed width, so "one more row would not have fit" is exactly
    `content_chars + row_width > limit`. Comparing against a re-fit of
    `items[:kept+1]` would be wrong: that result is untruncated, so it carries
    no warning and is smaller than the truncated candidate being tested.
    """
    items = [{"i": n, "pad": "c" * 128} for n in range(500)]
    limit = 20_000
    fitted = fit_json_result(
        items, build_payload=simple_builder(), narrowing_hint=HINT, max_content_chars=limit
    )
    assert fitted.truncated
    row_width = len(canonical_json(items[fitted.kept_count]))
    assert fitted.content_chars <= limit
    assert fitted.content_chars + row_width > limit


def test_a_bigger_budget_never_keeps_fewer_rows():
    items = [{"i": n, "pad": "m" * 128} for n in range(500)]
    kept = [
        fit_json_result(
            items, build_payload=simple_builder(), narrowing_hint=HINT, max_content_chars=limit
        ).kept_count
        for limit in (5_000, 10_000, 20_000, 40_000)
    ]
    assert kept == sorted(kept)


def test_non_ascii_is_measured_in_characters_and_bytes_separately():
    """A char budget is not a byte budget; both are reported, neither guessed."""
    items = [{"t": "日本語テキスト — ünïcodé"} for _ in range(50)]
    fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
    assert fitted.utf8_bytes > fitted.content_chars
    # ensure_ascii=False: multibyte characters must NOT be \u-escaped, or the
    # budget silently costs ~6x for every non-Latin script.
    assert "日本語" in fitted.serialized_text
    assert "\\u" not in fitted.serialized_text


# -- honest failure rather than empty success ---------------------------------


def test_single_oversized_item_raises_instead_of_returning_empty_success():
    """An empty `results` list reads as 'nothing matched'. It must not mean
    'one record was too big to emit' — that is a wrong answer, not a small one."""
    items = [{"body": "q" * 80_000}, {"body": "small"}]
    with pytest.raises(ResponseBudgetError, match="first result alone"):
        fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)


def test_fixed_metadata_over_budget_raises():
    huge = {"notes": ["n" * 1_000] * 100}
    with pytest.raises(ResponseBudgetError, match="fixed response metadata"):
        fit_json_result(
            [{"i": 1}],
            build_payload=simple_builder(huge),
            narrowing_hint=HINT,
            max_content_chars=5_000,
        )


def test_nan_fails_loudly_rather_than_emitting_invalid_json():
    with pytest.raises(ValueError):
        fit_json_result(
            [{"v": float("nan")}], build_payload=simple_builder(), narrowing_hint=HINT
        )


@pytest.mark.parametrize("bad", [10**9, "nonsense", None, 30_000.7, "25000"])
def test_budget_is_clamped_not_trusted(bad):
    """A caller-supplied budget is coerced and bounded; MAX is never exceeded."""
    items = [{"i": n} for n in range(5)]
    fitted = fit_json_result(
        items, build_payload=simple_builder(), narrowing_hint=HINT, max_content_chars=bad
    )
    limit = fitted.payload["metadata"]["response_budget"]["limit_content_chars"]
    assert 1 <= limit <= MAX_RESPONSE_CONTENT_CHARS
    assert fitted.content_chars <= limit


@pytest.mark.parametrize("tiny", [0, -1, 1, 50])
def test_a_budget_too_small_for_the_envelope_fails_loudly(tiny):
    """Better a loud error than a successful-looking empty envelope."""
    with pytest.raises(ResponseBudgetError, match="fixed response metadata"):
        fit_json_result(
            [{"i": 1}],
            build_payload=simple_builder(),
            narrowing_hint=HINT,
            max_content_chars=tiny,
        )


# -- preserved semantics ------------------------------------------------------


def test_pre_existing_coverage_and_warnings_survive_truncation():
    """Response-budget truncation must not erase why a scan was ALREADY partial."""

    def build(prefix, budget_meta, warning):
        warnings = ["upstream scan budget reached after 5,000 records"]
        if warning:
            warnings.append(warning)
        return {
            "results": list(prefix),
            "returned_count": len(prefix),
            "warnings": warnings,
            "metadata": {
                "coverage": {"status": "partial", "reason": "scan-budget", "next_cursor": "abc"},
                "response_budget": budget_meta,
            },
        }

    items = [{"i": n, "pad": "d" * 400} for n in range(500)]
    fitted = fit_json_result(items, build_payload=build, narrowing_hint=HINT)
    assert fitted.truncated
    coverage = fitted.payload["metadata"]["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["next_cursor"] == "abc"
    assert any("scan budget" in w for w in fitted.payload["warnings"])
    assert any("Response budget" in w for w in fitted.payload["warnings"])


# -- truncation grammar -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "raise max_results to see the rest",
        "Raise the cap for more rows",
        "increase max_results",
        "o subir max_results para ver el resto",
        "verhoog de limiet",
    ],
)
def test_banned_truncation_grammar_is_rejected_at_authoring_time(bad):
    with pytest.raises(ValueError, match="narrowing move"):
        assert_narrowing_grammar(bad)


@pytest.mark.parametrize(
    "good",
    [
        "Narrow the period or filter by state.",
        "Request the next page with metadata.coverage.next_offset.",
        "Select fewer fields, or fetch the document body separately.",
    ],
)
def test_narrowing_grammar_accepts_actionable_guidance(good):
    assert assert_narrowing_grammar(good) == good


def test_truncation_warning_names_the_narrowing_move():
    items = [{"i": n, "pad": "e" * 400} for n in range(500)]
    fitted = fit_json_result(items, build_payload=simple_builder(), narrowing_hint=HINT)
    warning = fitted.payload["warnings"][0]
    assert "next_offset" in warning
    assert str(len(items)) in warning


# -- text excerpting ----------------------------------------------------------


def test_excerpt_round_trips_by_following_offsets():
    text = "".join(f"line {n}\n" for n in range(5_000))
    rebuilt, offset, guard = "", 0, 0
    while offset is not None and guard < 100:
        chunk, offset = excerpt_text(text, offset_chars=offset, max_chars=5_000)
        rebuilt += chunk
        guard += 1
    assert rebuilt == text


def test_excerpt_reserves_room_for_wrapper_fields():
    text = "f" * 10_000
    chunk, _ = excerpt_text(text, max_chars=1_000, overhead_chars=200)
    assert len(chunk) == 800


@pytest.mark.parametrize(
    "filler", ["\n", '"', "\t", "\\", "\x07", "a"], ids=list("nqtbca")
)
def test_excerpt_fits_the_SERIALIZED_length_not_the_raw_one(filler):
    """JSON escaping expands text, and a raw slice overflows the budget.

    A newline costs two characters once encoded, a control character six. A
    markdown document is mostly newlines, so slicing raw characters overshoots
    by ~10% — the same class of mistake as counting rows instead of characters,
    one level down. Every filler here must still fit.
    """
    text = (filler + "x") * 20_000
    budget = 2_000
    chunk, next_offset = excerpt_text(text, max_chars=budget)
    assert len(canonical_json(chunk)) - 2 <= budget
    assert next_offset is not None


def test_excerpt_of_escape_heavy_text_still_round_trips():
    text = "".join(f'line {n} "quoted"\n\tindented\n' for n in range(2_000))
    rebuilt, offset, guard = "", 0, 0
    while offset is not None and guard < 200:
        chunk, offset = excerpt_text(text, offset_chars=offset, max_chars=3_000)
        assert len(canonical_json(chunk)) - 2 <= 3_000
        assert chunk, "a budget that cannot advance would loop forever"
        rebuilt += chunk
        guard += 1
    assert rebuilt == text


def test_excerpt_never_splits_a_surrogate_pair():
    text = "🇦🇺" * 500
    chunk, _ = excerpt_text(text, max_chars=101)
    assert chunk.encode("utf-8").decode("utf-8") == chunk


# -- the real FastMCP boundary ------------------------------------------------


async def test_example_list_result_fits_the_budget_through_the_client(client):
    """Measure what the CLIENT receives, not what the adapter built."""
    result = await client.call_tool(
        name="example_list", arguments={"query": "demo", "max_results": 500}
    )
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    assert len(text) <= DEFAULT_RESPONSE_CONTENT_CHARS
    assert json.loads(text) == result.structured_content


async def test_fs_read_pages_and_reconstructs_the_file(client):
    """Paging must reconstruct the file EXACTLY — the whole point of continuation."""
    path = "skills/server-guide/SKILL.md"
    whole = await client.call_tool(name="fs_read", arguments={"path": path})
    assert whole.data["truncated"] is False, "pick a file small enough to read in one page"
    expected = whole.data["content"]

    rebuilt, offset, guard = "", 0, 0
    page: dict = {}
    while offset is not None and guard < 200:
        result = await client.call_tool(
            name="fs_read",
            arguments={"path": path, "offset_chars": offset, "max_chars": 900},
        )
        page = result.data
        assert page["offset_chars"] == offset
        rebuilt += page["content"]
        offset = page["next_offset_chars"]
        guard += 1

    assert guard > 1, "budget too large to exercise continuation"
    assert page["truncated"] is False
    assert rebuilt == expected
    assert page["sha256"] == whole.data["sha256"]
    assert len(page["sha256"]) == 64


async def test_fs_read_default_response_fits_the_budget(client):
    result = await client.call_tool(
        name="fs_read", arguments={"path": "skills/server-guide/SKILL.md"}
    )
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    assert len(text) <= DEFAULT_RESPONSE_CONTENT_CHARS
>>>>>>> after updating
