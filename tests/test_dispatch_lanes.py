"""ARM vs queue dispatch lanes (ORG-PLAN-164 WS2).

The cutover switch. Both lanes ship simultaneously and the live one is a
server-owned setting, so what matters is that the ARM lane is byte-for-byte what
it always was (nothing changes on deploy) and that the queue lane never lets a
caller-shaped value into the message.
"""

from __future__ import annotations

import json

import pytest

from stromy_workflows_mcp import dispatch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingJobClient:
    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start(self, template: dict) -> dict:
        self.started.append(template)
        return {"id": "exec-1"}


class RecordingQueue:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, body: str) -> None:
        self.sent.append(body)


def test_mode_defaults_to_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_DISPATCH_MODE", raising=False)
    assert dispatch.dispatch_mode() == dispatch.MODE_ARM


def test_mode_reads_the_server_owned_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_DISPATCH_MODE", "queue")
    assert dispatch.dispatch_mode() == dispatch.MODE_QUEUE


def test_unknown_mode_fails_closed_to_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently mean 'queue'.

    Enqueueing to a queue nothing consumes yet leaves every run sitting
    `queued` with nothing anywhere saying why.
    """
    monkeypatch.setenv("WORKFLOW_DISPATCH_MODE", "kueue")
    assert dispatch.dispatch_mode() == dispatch.MODE_ARM


def test_build_dispatcher_honours_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_DISPATCH_MODE", "arm")
    assert isinstance(dispatch.build_dispatcher(RecordingJobClient()), dispatch.ArmDispatcher)


async def test_arm_lane_starts_the_job_with_its_template() -> None:
    client = RecordingJobClient()
    await dispatch.ArmDispatcher(client).dispatch(
        run_id="run-1", dispatch_id="d-1", template={"template": {"containers": []}}
    )
    assert client.started == [{"template": {"containers": []}}]


async def test_queue_lane_sends_references_only() -> None:
    """No config, no client slug, no template — the security property.

    Starting a job grants access to every secret configured on it, so a
    caller-shaped value must never ride in a dispatch.
    """
    queue = RecordingQueue()
    await dispatch.QueueDispatcher(queue).dispatch(
        run_id="11111111-1111-1111-1111-111111111111",
        dispatch_id="22222222-2222-2222-2222-222222222222",
        template={"template": {"containers": [{"env": [{"name": "SECRET"}]}]}},
    )

    assert len(queue.sent) == 1
    body = json.loads(queue.sent[0])
    assert body == {
        "version": 1,
        "dispatch_id": "22222222-2222-2222-2222-222222222222",
        "run_id": "11111111-1111-1111-1111-111111111111",
    }
    assert "SECRET" not in queue.sent[0]


async def test_queue_lane_never_starts_a_job() -> None:
    """The whole point: no Start API call, so no template override."""
    client = RecordingJobClient()
    await dispatch.QueueDispatcher(RecordingQueue()).dispatch(
        run_id="r", dispatch_id="d", template={"template": {}}
    )
    assert client.started == []


def test_encoding_matches_the_runner_contract() -> None:
    """Facade and runner encode/decode the same wire format.

    They live in different repos, so a drift here is a silent
    everything-poisons-on-decode outage rather than a build failure.
    """
    body = dispatch.encode("d-1", "r-1")
    assert json.loads(body) == {"version": 1, "dispatch_id": "d-1", "run_id": "r-1"}
    assert " " not in body  # compact separators on both sides


def test_queue_client_refuses_without_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_STORAGE_ACCOUNT", raising=False)
    with pytest.raises(dispatch.DispatchError, match="WORKFLOW_STORAGE_ACCOUNT"):
        dispatch.queue_client()
