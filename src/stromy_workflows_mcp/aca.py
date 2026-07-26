"""Azure Container Apps Job control-plane adapter."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

from .config import settings


class JobStartError(RuntimeError):
    pass


# ``POST .../jobs/<name>/start`` takes a ``JobExecutionTemplate`` at the TOP level —
# ``{"containers": [...], "initContainers": [...]}`` — not the job's own
# ``properties.template``, and not a ``{"template": ...}`` envelope (ARM answers that
# with `Unknown properties template in StartJobExecutionTemplate are not supported`).
# Its container schema is narrower than the job's, so the job template's read-only /
# unsupported members (``probes``, ``volumeMounts``, and the sibling ``volumes``) must
# be dropped rather than forwarded.
#
# Consequence worth knowing: a runner job that ever needs a mounted volume cannot be
# started with a per-execution override at all — the override schema has nowhere to
# carry one. Today the runner mounts nothing; the registry (Postgres) is its state.
_EXECUTION_CONTAINER_KEYS = ("image", "name", "command", "args", "env", "resources")


def _execution_container(container: dict[str, Any]) -> dict[str, Any]:
    return {
        key: container[key] for key in _EXECUTION_CONTAINER_KEYS if container.get(key) is not None
    }


@dataclass(frozen=True)
class PreparedJob:
    template: dict[str, Any]
    image_tag: str | None


class AcaJobClient:
    def __init__(self) -> None:
        if not settings.azure_subscription_id:
            raise JobStartError("AZURE_SUBSCRIPTION_ID is unset")
        self._credential = DefaultAzureCredential()

    @property
    def _resource_url(self) -> str:
        return (
            "https://management.azure.com/subscriptions/"
            f"{settings.azure_subscription_id}/resourceGroups/"
            f"{settings.azure_resource_group}/providers/Microsoft.App/jobs/"
            f"{settings.azure_runner_job}"
        )

    async def _headers(self) -> dict[str, str]:
        token = await asyncio.to_thread(self._credential.get_token, settings.azure_management_scope)
        return {"Authorization": f"Bearer {token.token}"}

    async def prepare(self, run_id: str) -> PreparedJob:
        """Read the server-owned job template and inject only ``run_id``.

        Returns a ``JobExecutionTemplate`` — the exact body ``start`` POSTs and the
        exact body persisted to ``runs.job_template_json``, so a resume replays it
        byte-identical.

        Caller config is deliberately not an argument. It lives in Postgres;
        starting a job grants the execution every secret configured on it, so no
        client-controlled string may enter this body.
        """
        params = {"api-version": settings.azure_api_version}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self._resource_url, params=params, headers=await self._headers()
            )
        if response.is_error:
            raise JobStartError(f"cannot read runner job template: {response.text}")
        job_template = copy.deepcopy(response.json().get("properties", {}).get("template"))
        if not isinstance(job_template, dict):
            raise JobStartError("runner job response has no properties.template")
        containers = job_template.get("containers")
        if not isinstance(containers, list) or not containers:
            raise JobStartError("runner job template has no containers")
        container = containers[0]
        if not isinstance(container, dict):
            raise JobStartError("runner job template container is malformed")
        execution_container = _execution_container(container)
        execution_container["command"] = ["python"]
        execution_container["args"] = ["-m", "stromy.runtime.worker", "--run-id", run_id]

        template: dict[str, Any] = {"containers": [execution_container]}
        init_containers = job_template.get("initContainers")
        if isinstance(init_containers, list) and init_containers:
            template["initContainers"] = [
                _execution_container(init) for init in init_containers if isinstance(init, dict)
            ]
        image = execution_container.get("image")
        return PreparedJob(template=template, image_tag=str(image) if image else None)

    async def start(self, template: dict[str, Any]) -> dict[str, Any]:
        params = {"api-version": settings.azure_api_version}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._resource_url}/start",
                params=params,
                headers=await self._headers(),
                json=template,
            )
        if response.is_error:
            raise JobStartError(f"runner job start failed: {response.text}")
        return response.json() if response.content else {"accepted": True}
