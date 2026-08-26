"""Server-side budgeting for the text an MCP tool result actually occupies.

**The budget is the complete MCP-visible text, and it is the server's job.**
See `infra-docs/ai/mcp-response-budgeting.md` (stromy-org) for the standard this
implements and the measurements behind its numbers.

Why this module exists, in one incident: on 2026-08-25 a client-facing tool
returned 200 rows — exactly its documented `max_results` cap — and the claude.ai
harness rejected the 70,681-character result outright. The cap was working as
designed. The design was wrong: **a row count does not bound output.** Rows vary
in width, and a row cap says nothing about the enclosing list, the envelope, the
warnings, the metadata, or the runtime's own serialization. Worse, the truncation
warning advised raising the cap, so the model raised it and made the next
response 179 KB.

Three rules follow, and this module exists to make them the path of least
resistance:

1. **Measure the final payload, serialized exactly as it will be emitted.**
   Never `len(json.dumps(row)) * n`. `fit_json_result` rebuilds and re-serializes
   the *whole* candidate payload on every probe, so warning and metadata overhead
   is inside the number being compared to the budget.
2. **Truncation is loud and names the narrowing move.** A response that silently
   drops rows is a correctness bug wearing a success code. `narrowing_hint` is
   mandatory, and phrasing that tells the model to raise a cap is rejected at
   authoring time (`assert_narrowing_grammar`) rather than shipped and greped for
   later.
3. **A single oversized item is an error, not an empty success.** Returning
   `results: []` with `truncated: true` is indistinguishable from "nothing
   matched" to a caller that reads the list. `ResponseBudgetError` names the
   offending item and what to do instead.

The two numbers are calibrated, not guessed. Anthropic documents a 25,000-token
default maximum for Claude Code MCP output; the observed claude.ai rejection
fired at ~70k characters. 40,000 characters keeps real margin under both while
staying far above any answer-shaped response. The 60,000 ceiling is an internal
validation bound for named complete/detail modes — it is deliberately NOT
exposed as a caller-settable `char_budget` parameter, because "let the model ask
for more" is the exact failure this module exists to prevent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_RESPONSE_CONTENT_CHARS",
    "MAX_RESPONSE_CONTENT_CHARS",
    "BudgetResult",
    "ResponseBudgetError",
    "assert_narrowing_grammar",
    "canonical_json",
    "excerpt_text",
    "fit_json_result",
]

DEFAULT_RESPONSE_CONTENT_CHARS = 40_000
MAX_RESPONSE_CONTENT_CHARS = 60_000

# Canonical MCP-visible serialization. FastMCP serializes a dict return value
# into TextContent with compact separators and ensure_ascii=False, and mirrors
# the same object into structuredContent — so this is the text the client sees,
# and measuring anything else measures the wrong thing. allow_nan=False because
# NaN/Infinity are not JSON and a client is entitled to reject them.
_JSON_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "separators": (",", ":"),
    "allow_nan": False,
}

# Warning phrasings that push the model in the direction that caused the
# incident. Matched case-insensitively across languages we actually ship in
# (the Spanish variant is not hypothetical — the anti-pattern spread by copy).
_BANNED_WARNING_GRAMMAR = re.compile(
    r"""
    (?: raise | increase | bump | subir | aumentar | verhoog | verhogen )
    \s+ (?: the \s+ | el \s+ | de \s+ )?
    (?: cap | limit | max_results | max \s+ results | l[ií]mite | tope | limiet )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ResponseBudgetError(ValueError):
    """A result cannot be made to fit by dropping whole items.

    Raised rather than returning an empty-but-successful envelope: an empty
    `results` list reads as "nothing matched", and a caller acting on that
    reports absence where the truth is "one record was too large to emit".
    """


def canonical_json(obj: Any) -> str:
    """Serialize exactly as the MCP runtime will. Use for every size decision."""
    return json.dumps(obj, **_JSON_KWARGS)


def assert_narrowing_grammar(text: str) -> str:
    """Reject truncation guidance that tells the caller to raise a cap.

    Called on every `narrowing_hint`, so the banned grammar fails at authoring
    time in the repo's own test run instead of surviving to a client surface.
    A truncation warning must name the *narrowing* move — a filter, a period, a
    field list, an offset — because the row cap is not the thing that was too
    big; the response was.
    """
    if _BANNED_WARNING_GRAMMAR.search(text):
        raise ValueError(
            "truncation guidance must name a narrowing move (a filter, period, "
            "field list, or offset), never suggest raising a cap or "
            f"max_results — got: {text!r}"
        )
    return text


@dataclass(frozen=True)
class BudgetResult:
    """The fitted payload plus the measurements that prove it fits."""

    payload: dict[str, Any]
    serialized_text: str
    kept_count: int
    dropped_count: int
    content_chars: int
    utf8_bytes: int

    @property
    def truncated(self) -> bool:
        return self.dropped_count > 0


def _clamp(max_content_chars: Any) -> int:
    """Coerce and bound the budget. Never trust a caller-supplied number."""
    try:
        value = int(max_content_chars)
    except (TypeError, ValueError):
        return DEFAULT_RESPONSE_CONTENT_CHARS
    return max(1, min(MAX_RESPONSE_CONTENT_CHARS, value))


def _budget_meta(*, limit: int, kept: int, dropped: int, text: str) -> dict[str, Any]:
    return {
        "limit_content_chars": limit,
        "content_chars": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "kept_count": kept,
        "dropped_count": dropped,
        "truncated": dropped > 0,
    }


def fit_json_result(
    items: Sequence[Any],
    *,
    build_payload: Callable[[Sequence[Any], dict[str, Any], str | None], dict[str, Any]],
    narrowing_hint: str,
    max_content_chars: int = DEFAULT_RESPONSE_CONTENT_CHARS,
) -> BudgetResult:
    """Return the largest stable prefix of `items` whose FINAL payload fits.

    `build_payload(prefix, budget_meta, warning)` must return the complete
    payload the tool would emit for that prefix — envelope, fixed metadata,
    pre-existing warnings and coverage, everything. It is called repeatedly, so
    it must be pure. `budget_meta` belongs at `metadata.response_budget`;
    `warning` (None when nothing was dropped) belongs in the warnings list.

    Two properties are load-bearing and easy to lose in a rewrite:

    - The candidate is **rebuilt and re-serialized on every probe**, so the
      warning text and the budget metadata — which only exist when the result
      *is* truncated, and which are themselves not free — are counted. A fit
      computed against the untruncated payload and then reused is a fit against
      a payload that was never emitted.
    - Truncation drops a **suffix of the given order**, never a sample. The
      caller's order must therefore be stable and meaningful, so that a
      continuation handle (`next_offset`, a cursor) actually resumes rather than
      re-rolling the dice.

    Raises `ResponseBudgetError` when even the empty prefix does not fit (fixed
    metadata alone exceeds the budget) or when the first item does not fit
    (an oversized single record — excerpt or externalize it upstream instead).
    """
    assert_narrowing_grammar(narrowing_hint)
    limit = _clamp(max_content_chars)
    total = len(items)

    def build(kept: int, *, truncated: bool) -> tuple[dict[str, Any], str]:
        """Build the payload for a prefix, resolving the self-size fixed point.

        `metadata.response_budget.content_chars` reports the size of the payload
        it is *inside*, so writing it changes it. Iterate to a fixed point rather
        than measuring once and mutating afterwards — mutating would leave
        `payload` and `serialized_text` describing different bytes, and the whole
        contract is that the emitted TextContent and structuredContent match.

        Convergence takes one or two rounds (only digit counts move). A digit
        rollover can leave a 2-cycle with the two candidates one character apart;
        in that case take the larger, so the declared size is an upper bound and
        never understates what the client received.
        """
        dropped = total - kept
        warning = None
        if truncated:
            warning = assert_narrowing_grammar(
                f"Response budget: showing the first {kept} of {total} results "
                f"({limit} characters). {narrowing_hint}"
            )
        meta = _budget_meta(limit=limit, kept=kept, dropped=dropped, text="")
        payload = build_payload(items[:kept], dict(meta), warning)
        text = canonical_json(payload)
        for _ in range(6):
            measured = _budget_meta(limit=limit, kept=kept, dropped=dropped, text=text)
            if measured == meta:
                return payload, text
            meta = {
                **measured,
                "content_chars": max(measured["content_chars"], meta["content_chars"]),
                "utf8_bytes": max(measured["utf8_bytes"], meta["utf8_bytes"]),
            }
            payload = build_payload(items[:kept], dict(meta), warning)
            text = canonical_json(payload)
        return payload, text

    payload, text = build(total, truncated=False)
    if len(text) <= limit:
        return BudgetResult(payload, text, total, 0, len(text), len(text.encode("utf-8")))

    _, empty_text = build(0, truncated=True)
    if len(empty_text) > limit:
        raise ResponseBudgetError(
            f"fixed response metadata is {len(empty_text)} characters, over the "
            f"{limit}-character budget before a single result is added — the "
            f"tool's own metadata or warnings must shrink. {narrowing_hint}"
        )

    # Binary search for the largest fitting prefix. Monotonic because each extra
    # item only adds characters (the warning text is present throughout the
    # searched range, since every candidate here is a truncated one).
    lo, hi = 0, total - 1
    best = 0
    best_payload, best_text = build(0, truncated=True)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate_payload, candidate_text = build(mid, truncated=True)
        if len(candidate_text) <= limit:
            best, best_payload, best_text = mid, candidate_payload, candidate_text
            lo = mid + 1
        else:
            hi = mid - 1

    if best == 0 and total:
        first_chars = len(canonical_json(items[0]))
        raise ResponseBudgetError(
            f"the first result alone serializes to {first_chars} characters and "
            f"does not fit the {limit}-character budget — excerpt the oversized "
            f"field, return it by reference, or select fewer fields. "
            f"{narrowing_hint}"
        )

    return BudgetResult(
        best_payload,
        best_text,
        best,
        total - best,
        len(best_text),
        len(best_text.encode("utf-8")),
    )


def excerpt_text(
    text: str,
    *,
    offset_chars: int = 0,
    max_chars: int = DEFAULT_RESPONSE_CONTENT_CHARS,
    overhead_chars: int = 0,
) -> tuple[str, int | None]:
    """Slice long text for a budgeted complete-mode read.

    Returns `(chunk, next_offset_chars)` where `next_offset_chars` is None once
    the end is reached. `overhead_chars` reserves room for the fields wrapped
    around the chunk, so the caller's *final* result — not just the chunk — is
    what fits.

    **The slice is fitted to the chunk's SERIALIZED length, not its raw one.**
    JSON escaping expands text, and by exactly the amount that matters here: a
    newline costs two characters, a quote two, a control character six. A
    markdown document is mostly newlines, so a naive `text[start:start+budget]`
    overflows the budget by ~10% — measured, on the first long page this was
    tested against. Raw-character slicing is the same class of mistake as
    counting rows instead of characters, one level down.

    Slicing is on Python string indices, i.e. code points, so a surrogate pair
    is never split; grapheme clusters may still be, which is acceptable for a
    resumable read and is why the offset is returned rather than inferred.
    """
    budget = max(1, _clamp(max_chars) - max(0, overhead_chars))
    start = max(0, int(offset_chars))
    remaining = text[start:]

    if _encoded_len(remaining) <= budget:
        chunk = remaining
    else:
        # Largest prefix whose escaped form fits. Monotonic in length, so a
        # binary search is exact rather than an approximation with a fudge
        # factor — and a fudge factor is what would rot silently.
        lo, hi, best = 0, len(remaining), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if _encoded_len(remaining[:mid]) <= budget:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        chunk = remaining[:best]

    end = start + len(chunk)
    return chunk, (end if end < len(text) else None)


def _encoded_len(chunk: str) -> int:
    """Characters this string occupies once JSON-encoded, quotes excluded."""
    return len(canonical_json(chunk)) - 2
