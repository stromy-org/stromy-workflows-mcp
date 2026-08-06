"""Blob access for the inbound upload path (ORG-PLAN-164 WS3).

Two capabilities, deliberately asymmetric:

* **write** — a short-lived, single-blob SAS handed to a browser so it can PUT
  one declared file straight to storage. The bytes never transit the facade.
* **read** — the facade's own managed-identity read of a staged object, used at
  finalization to verify what was *actually* uploaded.

Why a user-delegation SAS and not an account-key SAS
----------------------------------------------------
An account-key SAS is signed with the storage account key, so it inherits the
key's whole blast radius and cannot be revoked without rotating that key — which
would simultaneously break the ACA Files mount that also needs it. A
user-delegation SAS is signed with an Entra-issued key bound to the facade's
managed identity, expires with that key, and is revocable by removing the
identity's role assignment. It is also the only form that keeps the account key
out of the request path entirely.

The SAS is scoped as narrowly as the API allows: one blob, ``create``+``write``
only (never ``read``, never ``delete``, never ``list``), and minutes of validity.
A caller who captures one can overwrite exactly the object they were already
authorised to upload, in a staging prefix, for a session that has not finalized.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Long enough for a slow mobile upload of a file at the per-file ceiling, short
#: enough that a leaked URL is not a lasting capability.
UPLOAD_SAS_TTL_SECONDS = int(os.environ.get("WORKFLOW_UPLOAD_SAS_TTL_SECONDS", "900"))

#: Clock skew allowance. Azure rejects a SAS whose start time is in the future
#: relative to ITS clock, so a naive `start = now` fails intermittently.
_CLOCK_SKEW = timedelta(minutes=5)


class BlobError(RuntimeError):
    """Blob configuration or access failed."""


@dataclass(frozen=True)
class UploadTarget:
    """One browser-uploadable slot."""

    ordinal: int
    display_name: str
    media_type: str
    url: str
    expires_at: str


class UploadUrlMinter(Protocol):
    def mint(self, *, blob_key: str, media_type: str) -> tuple[str, datetime]: ...


def storage_account() -> str:
    """The one data-plane account. Inputs and outputs are containers on it.

    Not ``input_account``: the same account holds the output container, and a
    name implying otherwise invites a second, divergent accessor for the
    outbound half.
    """
    account = os.environ.get("WORKFLOW_STORAGE_ACCOUNT", "").strip()
    if not account:
        raise BlobError("WORKFLOW_STORAGE_ACCOUNT is unset; uploads have nowhere to go")
    return account


def input_container() -> str:
    return os.environ.get("WORKFLOW_INPUT_CONTAINER", "workflow-inputs").strip()


def output_container() -> str:
    return os.environ.get("WORKFLOW_OUTPUT_CONTAINER", "workflow-outputs").strip()


class AzureUploadUrlMinter:
    """Mints per-blob write SAS URLs signed with a user-delegation key."""

    def __init__(self, *, account: str | None = None, container: str | None = None) -> None:
        self._account = account or storage_account()
        self._container = container or input_container()
        try:
            from azure.identity import DefaultAzureCredential  # noqa: PLC0415
            from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise BlobError(
                "azure-storage-blob/azure-identity are required to mint upload URLs"
            ) from exc
        self._service = BlobServiceClient(
            account_url=f"https://{self._account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )

    def mint(self, *, blob_key: str, media_type: str) -> tuple[str, datetime]:
        from azure.storage.blob import (  # noqa: PLC0415
            BlobSasPermissions,
            generate_blob_sas,
        )

        start = datetime.now(UTC) - _CLOCK_SKEW
        expiry = datetime.now(UTC) + timedelta(seconds=UPLOAD_SAS_TTL_SECONDS)
        # The delegation key's own lifetime bounds every SAS signed with it, so
        # it is requested per mint rather than cached across sessions.
        delegation_key = self._service.get_user_delegation_key(start, expiry)
        token = generate_blob_sas(
            account_name=self._account,
            container_name=self._container,
            blob_name=blob_key,
            user_delegation_key=delegation_key,
            # create+write only. Notably NOT read: the browser must not be able
            # to read back another file in the same staging prefix, and NOT
            # delete: an upload capability is not a destruction capability.
            permission=BlobSasPermissions(create=True, write=True),
            start=start,
            expiry=expiry,
            # Pinned at signing time so the stored object's type is decided by
            # the server's allowlist, not by whatever header the browser sends.
            content_type=media_type,
        )
        url = (
            f"https://{self._account}.blob.core.windows.net/"
            f"{self._container}/{blob_key}?{token}"
        )
        return url, expiry


class AzureStagedReader:
    """Reads staged bytes back for content verification at finalization."""

    def __init__(self, *, account: str | None = None, container: str | None = None) -> None:
        self._account = account or storage_account()
        self._container_name = container or input_container()
        try:
            from azure.identity import DefaultAzureCredential  # noqa: PLC0415
            from azure.storage.blob import ContainerClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise BlobError(
                "azure-storage-blob/azure-identity are required to read uploads"
            ) from exc
        self._container = ContainerClient(
            account_url=f"https://{self._account}.blob.core.windows.net",
            container_name=self._container_name,
            credential=DefaultAzureCredential(),
        )

    def __call__(self, storage_key: str) -> bytes:
        return self._container.download_blob(storage_key).readall()


def build_upload_targets(
    accepted: Any, minter: UploadUrlMinter
) -> list[UploadTarget]:
    """Mint one write URL per accepted file, in declared order."""
    targets: list[UploadTarget] = []
    for item in accepted:
        url, expiry = minter.mint(blob_key=item.storage_name, media_type=item.media_type)
        targets.append(
            UploadTarget(
                ordinal=item.ordinal,
                display_name=item.display_name,
                media_type=item.media_type,
                url=url,
                expires_at=expiry.isoformat(),
            )
        )
    return targets
