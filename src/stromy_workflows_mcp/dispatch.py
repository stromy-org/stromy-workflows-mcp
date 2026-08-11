"""Run dispatch, with an ARM lane and a queue lane (ORG-PLAN-164 WS2).

The cutover mechanism. Both lanes exist simultaneously and the live one is a
server-owned setting (``WORKFLOW_DISPATCH_MODE``), so switching is a config
change an operator can make and reverse — not a deploy.

Why the queue lane has to exist at all
--------------------------------------
The ARM lane starts a Manual job with a per-execution ``JobExecutionTemplate``
override, which is the only way it can inject ``--run-id``. Microsoft documents
that such an override REPLACES the complete execution template, and the Start
API's template schema has no ``volumes``/``volumeMounts`` surface at all. So a
mounted job cannot be started this way — not "is tricky to", *cannot*. The queue
lane exists because the runner needs its Azure Files mount, and an Event job is
the shape that keeps its full base template.

The message carries a version, an opaque dispatch id, and the registry-minted
run id. Nothing else. Everything the runner needs it resolves from Postgres
against that run id, which is what keeps caller-shaped values out of a job
template — starting a job grants access to every secret configured on it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DISPATCH_VERSION = 1

MODE_ARM = "arm"
MODE_QUEUE = "queue"


class DispatchError(RuntimeError):
    """Dispatch could not be delivered. Never degrades to a silent no-op."""


def encode(dispatch_id: str, run_id: str) -> str:
    """Byte-identical to the runner's ``stromy.runtime.dispatch.encode``."""
    return json.dumps(
        {"version": DISPATCH_VERSION, "dispatch_id": dispatch_id, "run_id": run_id},
        separators=(",", ":"),
    )


def new_dispatch_id() -> str:
    return str(uuid.uuid4())


class Dispatcher(Protocol):
    async def dispatch(
        self, *, run_id: str, dispatch_id: str, template: dict[str, Any]
    ) -> None: ...


class ArmDispatcher:
    """Legacy lane: start the Manual job with a per-execution template override."""

    def __init__(self, job_client: Any) -> None:
        self._job_client = job_client

    async def dispatch(self, *, run_id: str, dispatch_id: str, template: dict[str, Any]) -> None:
        await self._job_client.start(template)


class QueueDispatcher:
    """Target lane: enqueue a reference and let the Event job pick it up."""

    def __init__(self, queue: Any) -> None:
        self._queue = queue

    async def dispatch(self, *, run_id: str, dispatch_id: str, template: dict[str, Any]) -> None:
        import asyncio  # noqa: PLC0415 - only the queue lane needs the thread hop

        await asyncio.to_thread(self._queue.send_message, encode(dispatch_id, run_id))


def dispatch_mode() -> str:
    """Live dispatch lane. Unknown values fail closed to ARM.

    Fail-closed matters here: a typo'd mode that silently meant "queue" would
    enqueue messages no job is consuming yet, and every run would sit `queued`
    with nothing saying why.
    """
    mode = os.environ.get("WORKFLOW_DISPATCH_MODE", MODE_ARM).strip().lower()
    if mode not in {MODE_ARM, MODE_QUEUE}:
        logger.error("unknown WORKFLOW_DISPATCH_MODE %r; falling back to %r", mode, MODE_ARM)
        return MODE_ARM
    return mode


def queue_client() -> Any:
    """Azure Storage Queue client authenticated by the facade's managed identity."""
    account = os.environ.get("WORKFLOW_STORAGE_ACCOUNT", "").strip()
    queue_name = os.environ.get("WORKFLOW_DISPATCH_QUEUE", "workflow-runs").strip()
    if not account:
        raise DispatchError(
            "WORKFLOW_STORAGE_ACCOUNT is unset; queue dispatch has no account to send to"
        )
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        from azure.storage.queue import QueueClient  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise DispatchError(
            "azure-storage-queue/azure-identity are required for queue dispatch"
        ) from exc
    return QueueClient(
        account_url=f"https://{account}.queue.core.windows.net",
        queue_name=queue_name,
        credential=DefaultAzureCredential(),
    )


def build_dispatcher(job_client: Any, *, mode: str | None = None) -> Dispatcher:
    resolved = mode or dispatch_mode()
    if resolved == MODE_QUEUE:
        return QueueDispatcher(queue_client())
    return ArmDispatcher(job_client)
