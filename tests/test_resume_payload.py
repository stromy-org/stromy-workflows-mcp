"""What the facade accepts as an answer to a human-review interrupt.

The resume payload had no validation of any kind: whatever a caller sent was
persisted verbatim into ``config_json._resume`` and replayed into the graph in a
later container. Two properties belong to this boundary specifically, because it
is the one that owns the column:

* it is a JSON **object** — anything else cannot be merged into review state at
  all, and storing it only defers the failure into a container;
* it is **bounded** — otherwise a tool whose purpose is to answer a question is
  also a way to write arbitrarily large values into the registry.

What is deliberately NOT here: confining the file paths a payload may name. That
is a property of what the graph does with them, and it is enforced in Stromy's
own review-path resolver, against that run's workspace — a fact this layer does
not have.
"""

from __future__ import annotations

import pytest

from stromy_workflows_mcp.contracts import ConfigRejected
from stromy_workflows_mcp.service import (
    MAX_RESUME_PAYLOAD_BYTES,
    _validate_resume_payload,
)


def test_an_ordinary_review_answer_is_accepted() -> None:
    _validate_resume_payload(
        {
            "decision_summary": "revised after review",
            "run_flags": {"deep_research": True},
            "realities_payload": {"stakeholders": [{"display_name": "Employees"}]},
        }
    )


def test_an_empty_payload_is_accepted() -> None:
    """Resuming with no changes is the normal case — accept as-is."""
    _validate_resume_payload({})
    _validate_resume_payload(None)


@pytest.mark.parametrize("payload", ["a string", 42, ["a", "list"], True])
def test_a_non_object_payload_is_refused(payload: object) -> None:
    with pytest.raises(ConfigRejected, match="must be a JSON object"):
        _validate_resume_payload(payload)


def test_an_unserializable_payload_is_refused() -> None:
    with pytest.raises(ConfigRejected, match="JSON-serializable"):
        _validate_resume_payload({"when": object()})


def test_an_oversized_payload_is_refused() -> None:
    """The registry column is not a bulk channel."""
    payload = {"notes": "x" * (MAX_RESUME_PAYLOAD_BYTES + 1)}
    with pytest.raises(ConfigRejected, match="above the"):
        _validate_resume_payload(payload)


def test_a_payload_just_under_the_ceiling_is_accepted() -> None:
    """The bound exists to stop abuse, not to make a real questionnaire fail."""
    _validate_resume_payload({"notes": "x" * (MAX_RESUME_PAYLOAD_BYTES - 100)})
