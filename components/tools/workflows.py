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
    """List hosted workflows and the caller-visible configuration contract.

    Client callers see tier-1 interview questions and tier-2 defaults. Provider-
    locked tier-3 keys are visible only to the operator role.
    """
    try:
        return await asyncio.to_thread(service.list_workflows, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def describe_workflow(name: str) -> dict[str, Any]:
    """Describe one hosted workflow's tiered configuration contract."""
    try:
        return await asyncio.to_thread(service.describe_workflow, name, identity.caller_scope())
    except Exception as exc:
        raise _error(exc) from exc


@tool
async def validate_config(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a workflow configuration without starting it."""
    try:
        return await asyncio.to_thread(
            service.validate_config, name, config, identity.caller_scope()
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
