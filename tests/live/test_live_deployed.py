"""Deployed-endpoint smoke — the composition canary.

Unit CI proves the parts; nothing else observes the composed product — the
deployed container answering real MCP calls. Cross-cutting changes (config,
transport, budgeting, a published artifact, an upstream contract) compose
ONLY at the deployed surface (org doctrine:
infra-docs/ai/published-artifact-contracts.md), so that surface gets its own
daily probe: MCP-over-HTTP against production, exactly as a client calls it.

Run by `live-deployed.yml` (`uv run pytest -m deployed`) with `MCP_URL` set
to the production endpoint. The scaffold ships two server-local golden paths
(no upstream involved); ADD golden paths for your domain surface — the served
artifact end to end, one real upstream data path, any product property whose
silent loss would be user-facing.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest

MCP_URL = os.environ.get("MCP_URL", "")

pytestmark = [
    pytest.mark.deployed,
    pytest.mark.skipif(not MCP_URL, reason="MCP_URL not set (pre-first-deploy)"),
]

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
# Scale-to-zero: the first request of the day pays the cold start.
_TIMEOUT = 180.0


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Parse a streamable-HTTP response: SSE `data:` frames or plain JSON."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        frames = [
            line[5:].strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        assert frames, f"no SSE data frame in response: {response.text[:200]!r}"
        return json.loads(frames[-1])
    return response.json()


def _initialize(client: httpx.Client) -> dict[str, str]:
    """MCP handshake; returns the per-session headers. One retry for cold start."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "live-deployed-smoke", "version": "1"},
        },
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = client.post(MCP_URL, headers=_HEADERS, json=payload)
            resp.raise_for_status()
            headers = dict(_HEADERS)
            sid = resp.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            client.post(
                MCP_URL,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            return headers
        except (httpx.HTTPError, AssertionError) as exc:  # cold start / transient
            last_exc = exc
            if attempt == 1:
                time.sleep(20)
    raise AssertionError(f"MCP initialize failed twice against {MCP_URL}: {last_exc}")


def _rpc(
    client: httpx.Client, headers: dict[str, str], method: str, params: dict[str, Any]
) -> dict[str, Any]:
    resp = client.post(
        MCP_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    )
    resp.raise_for_status()
    body = _parse(resp)
    assert "error" not in body, f"JSON-RPC error from {method}: {body['error']}"
    return body.get("result") or {}


def mcp_call(tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """One tool call; returns (structuredContent, serialized text content)."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        headers = _initialize(client)
        result = _rpc(client, headers, "tools/call", {"name": tool, "arguments": arguments})
    text = next(
        (b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text"),
        "",
    )
    assert not result.get("isError"), f"{tool} returned isError: {text[:400]}"
    return result.get("structuredContent") or {}, text


def test_server_lists_tools() -> None:
    """The deployed server answers the MCP handshake and exposes its surface."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        headers = _initialize(client)
        result = _rpc(client, headers, "tools/list", {})
    assert result.get("tools"), "deployed server lists no tools"


def test_hosted_skills_are_served() -> None:
    """The server-local surface works: shipped skills are listed and readable.

    Zero upstream involved — a failure here is the service itself (image,
    filesystem, transport), not anything external.
    """
    sc, text = mcp_call("fs_list", {"path": "skills"})
    haystack = text or json.dumps(sc)
    assert haystack.strip(), "fs_list('skills') returned nothing"
