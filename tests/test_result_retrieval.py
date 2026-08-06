"""Result retrieval mints download URLs per authorized read (ORG-PLAN-164 WS4).

The registry stores URL-free descriptors on purpose. Two failure modes sit on
either side of that decision, and both are invisible until a client is affected:

* persist a URL at publication time -> it expires, and a *completed* run becomes
  permanently unfetchable;
* persist a long-lived URL -> it is a standing unauthenticated capability sitting
  in whatever transcript returned it.

So the URL is created here, after the scope check, and kept short. These tests
pin that the mint happens per call, that it happens only for a caller who owns
the run, and that one unmintable artifact does not make the whole result
unreadable.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from stromy_workflows_mcp import registry, service
from stromy_workflows_mcp.scoping import CallerScope

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
RUN = "11111111-1111-1111-1111-111111111111"

OWNER_SCOPE = CallerScope(frozenset({"dukestrategies"}))
OTHER_SCOPE = CallerScope(frozenset({"someoneelse"}))


def _run(**overrides: Any) -> registry.Run:
    published = [
        {
            "artifact_id": "report_pdf",
            "filename": "stakeholder-analysis.pdf",
            "media_type": "application/pdf",
            "size_bytes": 2048,
            "sha256": "a" * 64,
            "container": "workflow-outputs",
            "blob_key": f"{RUN}/report_pdf/stakeholder-analysis.pdf",
        }
    ]
    row: dict[str, Any] = {
        "run_id": RUN,
        "workflow": "stakeholder_analysis_workflow",
        "thread_id": RUN,
        "status": "completed",
        "client_slug": "dukestrategies",
        "config_json": {},
        "image_tag": "sha-abc",
        "job_template_json": None,
        "created_at": NOW,
        "updated_at": NOW,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": {"published": published},
        "idempotency_key": None,
    }
    row.update(overrides)
    # Built through from_row rather than the constructor: that is the mapper the
    # facade actually uses, so this fixture cannot drift from the real shape.
    return registry.Run.from_row(row)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Serve a fixed run and record every mint call."""

    def _install(*, run: Any, mint: Any = "https://acct.blob.core.windows.net/x?sig=fresh"):
        @contextmanager
        def _connect(*_a: Any, **_k: Any):
            yield object()

        monkeypatch.setattr(registry, "connect", _connect)
        monkeypatch.setattr(registry, "get_run", lambda _conn, _run_id: run)
        monkeypatch.setenv("WORKFLOW_STORAGE_ACCOUNT", "ststromyworkflows")

        mints: list[dict[str, Any]] = []

        def _mint(**kwargs: Any) -> str:
            mints.append(kwargs)
            if isinstance(mint, Exception):
                raise mint
            return mint

        import stromy_asset_transport.publication as publication

        monkeypatch.setattr(publication, "mint_download_url", _mint)
        return mints

    return _install


def test_a_fresh_url_is_minted_for_each_published_artifact(patched) -> None:
    mints = patched(run=_run())

    result = service.get_results(RUN, OWNER_SCOPE)

    artifact = result["artifacts"]["published"][0]
    assert artifact["download_url"] == "https://acct.blob.core.windows.net/x?sig=fresh"
    assert artifact["download_url_ttl_seconds"] == service.DOWNLOAD_URL_TTL_SECONDS
    # Minted against the account the FACADE owns, named explicitly rather than
    # inherited from asset-transport's own ASSET_STORE_ACCOUNT env var.
    assert mints[0]["account"] == "ststromyworkflows"
    assert mints[0]["container"] == "workflow-outputs"
    assert mints[0]["blob_key"] == f"{RUN}/report_pdf/stakeholder-analysis.pdf"


def test_the_stored_descriptor_is_never_mutated_with_a_url(patched) -> None:
    """The row keeps its URL-free form; only the response carries a capability."""
    run = _run()
    patched(run=run)

    service.get_results(RUN, OWNER_SCOPE)

    assert run.artifacts_json is not None
    stored = run.artifacts_json["published"][0]
    assert "download_url" not in stored


def test_no_url_is_minted_for_a_caller_outside_the_runs_scope(patched) -> None:
    """The scope check gates minting, not just the read."""
    mints = patched(run=_run())

    with pytest.raises(PermissionError):
        service.get_results(RUN, OTHER_SCOPE)

    assert mints == []


def test_one_unmintable_artifact_does_not_sink_the_whole_result(patched) -> None:
    """The client still learns the artifact exists, with its digest and size."""
    patched(run=_run(), mint=RuntimeError("delegation key request failed"))

    result = service.get_results(RUN, OWNER_SCOPE)

    artifact = result["artifacts"]["published"][0]
    assert "download_url" not in artifact
    assert artifact["sha256"] == "a" * 64
    assert artifact["size_bytes"] == 2048
    assert result["status"] == "completed"


def test_a_run_with_no_published_artifacts_is_returned_unchanged(patched) -> None:
    mints = patched(run=_run(artifacts_json=None))

    result = service.get_results(RUN, OWNER_SCOPE)

    assert result["artifacts"] == {}
    assert mints == []


def test_a_descriptor_with_no_blob_key_is_skipped_not_crashed(patched) -> None:
    """Defensive: a legacy or malformed row must not break result retrieval."""
    mints = patched(run=_run(artifacts_json={"published": [{"artifact_id": "orphan"}]}))

    result = service.get_results(RUN, OWNER_SCOPE)

    assert result["artifacts"]["published"] == [{"artifact_id": "orphan"}]
    assert mints == []
