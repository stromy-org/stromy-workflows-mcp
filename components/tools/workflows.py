"""Client-facing workflow discovery and run-lifecycle tools."""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool

from stromy_workflows_mcp import identity, service


def _error(exc: Exception) -> ToolError:
    return ToolError(str(exc))


@tool
async def list_workflows() -> list[dict[str, Any]]:
    """List hosted workflows the caller may run, with their interview questions.

    A summary for CHOOSING a workflow. Call ``describe_workflow`` for the tiered
    contract needed to configure one.
    """
    try:
        return await asyncio.to_thread(service.list_workflows, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def describe_workflow(name: str) -> dict[str, Any]:
    """Describe one hosted workflow's tiered configuration contract.

    Client callers see tier-1 interview questions and tier-2 defaults. Provider-
    locked tier-3 keys are visible only to the operator role.
    """
    try:
        return await asyncio.to_thread(service.describe_workflow, name, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def validate_config(
    name: str,
    config: dict[str, Any],
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a workflow configuration without starting it.

    Returns ``{"config": …}`` — the normalized configuration to submit verbatim to
    ``start_run``. Pass the same ``client_context`` you intend to start with and the
    reply also carries the resolved ``client_slug``: the server-confirmed run owner,
    which determines the deliverable's brand. Show that echoed value in the
    pre-flight confirmation block rather than the slug you resolved locally.
    """
    try:
        return await asyncio.to_thread(
            service.validate_config, name, config, identity.caller_scope(), client_context
        )
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def start_run(
    name: str,
    config: dict[str, Any],
    client_context: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Validate a config, register a run, and start its isolated ACA Job execution.

    ``client_context.client_slug`` selects one of the caller's verified
    ``client.<slug>`` app roles; it never grants scope by itself. Reusing an
    idempotency key returns the original run instead of launching a duplicate.
    """
    try:
        return await service.start_run(
            name,
            config,
            client_context,
            idempotency_key,
            identity.caller_scope(),
        )
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def create_input_session(
    files: list[dict[str, Any]],
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open an input session for client documents — browser upload, inline text, or both.

    Each file takes one of two shapes:

    * **Inline** ``{"name": "briefing.md", "content": "<the full text>"}`` — for
      .md/.txt content you already hold, e.g. a briefing drafted and reviewed in
      this conversation. Omit ``size_bytes`` (it is derived). Staged
      immediately; no browser step for these files. Capped at ~128 KB per file.
    * **Declared** ``{"name": "brief.pdf", "size_bytes": 12345}`` — for files
      only the person has (PDFs, anything large). ``size_bytes`` MUST be the
      exact byte size of the file that will be uploaded — finalization rejects
      any mismatch — so if you cannot measure the file, ask the person to
      attach it to the conversation first, or supply text inline instead.

    Returns an ``inputset:`` handle; when any file is declared, also a one-time
    ``upload_url`` for a browser — give that link to the person who has the
    documents, since those bytes travel from their browser straight to storage
    and never through this server or the agent. Accepted types: .md, .txt, .pdf.

    Call ``finalize_input_session`` once every declared file is uploaded (or
    immediately when everything was inline), then pass the handle as the
    workflow's input field.
    """
    try:
        return await asyncio.to_thread(
            service.create_input_session, files, client_context, identity.caller_scope()
        )
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def get_input_session(handle: str) -> dict[str, Any]:
    """Report upload progress for one ``inputset:`` handle the caller owns.

    Never returns the upload token or any storage URL — only per-file status.
    """
    try:
        return await asyncio.to_thread(
            service.get_input_session, handle, identity.caller_scope()
        )
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def finalize_input_session(handle: str) -> dict[str, Any]:
    """Verify the staged bytes and close an input session.

    Checks each file's real content against what the session declared — browser
    uploads and inline files alike — then revokes the upload link. Only a
    finalized handle can be attached to a run. Safe to call more than once.
    """
    try:
        return await asyncio.to_thread(
            service.finalize_input_session, handle, identity.caller_scope()
        )
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def run_status(run_id: str) -> dict[str, Any]:
    """Return one caller-scoped run, including an HITL interrupt payload."""
    try:
        return await asyncio.to_thread(service.run_status, run_id, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """List recent runs visible to the caller's verified client roles."""
    try:
        return await asyncio.to_thread(service.list_runs, identity.caller_scope(), limit)
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def resume_run(run_id: str, resume_payload: Any) -> dict[str, Any]:
    """Resume a paused HITL run using its exact stored job template."""
    try:
        return await service.resume_run(run_id, resume_payload, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def retry_run(run_id: str) -> dict[str, Any]:
    """Start a fresh attempt of a FAILED run, reusing its durable workspace.

    Returns a new ``run_id`` — a retry is a new run with new progress and a new
    checkpoint, linked to the original through ``attempt``. Whatever completed
    stages already wrote is reused rather than recomputed, so a retry after a
    late-stage failure is usually much cheaper than starting over.

    Takes no client context and no config: the owner, the workflow and the
    configuration all come from the original run. Only a ``failed`` run can be
    retried — a ``paused`` one resumes (``resume_run``), and a ``completed`` one
    already has its results (``get_results``).
    """
    try:
        return await service.retry_run(run_id, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def cancel_run(run_id: str) -> dict[str, Any]:
    """Cancel a queued or paused caller-scoped run."""
    try:
        return await asyncio.to_thread(service.cancel_run, run_id, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def get_results(run_id: str) -> dict[str, Any]:
    """Return the artifact index for one caller-scoped run."""
    try:
        return await asyncio.to_thread(service.get_results, run_id, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc
