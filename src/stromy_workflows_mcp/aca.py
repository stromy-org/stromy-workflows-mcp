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

        Caller config is deliberately not an argument. It lives in Postgres;
        the template can expose configured job secrets to the process, so no
        client-controlled string may enter this body.
        """
        params = {"api-version": settings.azure_api_version}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self._resource_url, params=params, headers=await self._headers()
            )
        if response.is_error:
            raise JobStartError(f"cannot read runner job template: {response.text}")
        template = copy.deepcopy(response.json().get("properties", {}).get("template"))
        if not isinstance(template, dict):
            raise JobStartError("runner job response has no properties.template")
        containers = template.get("containers")
        if not isinstance(containers, list) or not containers:
            raise JobStartError("runner job template has no containers")
        container = containers[0]
        if not isinstance(container, dict):
            raise JobStartError("runner job template container is malformed")
        container["command"] = ["python"]
        container["args"] = ["-m", "stromy.runtime.worker", "--run-id", run_id]
        image = container.get("image")
        return PreparedJob(template=template, image_tag=str(image) if image else None)

    async def start(self, template: dict[str, Any]) -> dict[str, Any]:
        params = {"api-version": settings.azure_api_version}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._resource_url}/start",
                params=params,
                headers=await self._headers(),
                json={"template": template},
            )
        if response.is_error:
            raise JobStartError(f"runner job start failed: {response.text}")
        return response.json() if response.content else {"accepted": True}
