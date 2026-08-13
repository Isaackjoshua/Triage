"""The service surface: session creation, out-of-band approval, and the journal endpoint.

The approval flow is the interesting one. At the terminal the handler blocks on input();
here it parks on a future and the session stays paused until a POST arrives — so the same
guarantee has to hold: nothing is applied while anyone is still deciding.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from fakes import DynamicClient, Response, finalize, finding, propose, read

from triage.api import app as api_module
from triage.api.app import SESSIONS, app


@pytest.fixture(autouse=True)
def clean_registry():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


@pytest.fixture
def client():
    # Context-managed so one event loop persists across requests: the session runs as a
    # background task on it, and a per-request loop would kill it the moment POST returns.
    with TestClient(app) as test_client:
        yield test_client


def _script():
    return [
        lambda s: Response([read("c1", "df -h", "filesystem usage")]),
        lambda s: Response(
            [
                finding(
                    "f1",
                    "/var is 100% full",
                    "df: /dev/sdb1 100% /var",
                    "an unrotated log filled it",
                    "high",
                    "software_fixable",
                )
            ]
        ),
        lambda s: Response(
            [propose("p1", "truncate -s 0 /var/log/ledger/debug.log", s.findings[0].id)]
        ),
        lambda s: Response([read("c2", "df -h", "verify")]),
        lambda s: Response([finalize("r1")]),
        lambda s: Response([], stop_reason="end_turn"),
    ]


def _patch_client(monkeypatch) -> None:
    """Give every session created by the service a scripted model."""
    original = api_module.DiagnosticSession

    def factory(*args, **kwargs):
        holder: dict = {}
        kwargs["client"] = DynamicClient(_script(), lambda: holder["session"])
        session = original(*args, **kwargs)
        holder["session"] = session
        return session

    monkeypatch.setattr(api_module, "DiagnosticSession", factory)


def test_a_session_cannot_be_created_without_an_authorization_assertion(
    client: TestClient,
) -> None:
    response = client.post(
        "/sessions", json={"authorization": "   ", "asserted_by": "operator", "mock": True}
    )
    assert response.status_code == 400
    assert "authorized to service" in response.json()["detail"]


def test_a_session_needs_a_target(client: TestClient) -> None:
    response = client.post(
        "/sessions", json={"authorization": "I own it", "asserted_by": "operator"}
    )
    assert response.status_code == 400
    assert "mock" in response.json()["detail"]


def test_the_full_flow_over_http(client: TestClient, monkeypatch, tmp_path) -> None:
    _patch_client(monkeypatch)

    created = client.post(
        "/sessions",
        json={
            "authorization": "I administer this machine (asset #4412).",
            "asserted_by": "operator",
            "mock": True,
            "journal_path": str(tmp_path / "api.sqlite"),
        },
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    # The loop runs in the background and parks on the pending approval.
    pending = _await_pending(client, session_id)
    assert pending["awaiting_decision"] is True
    assert pending["command"] == "truncate -s 0 /var/log/ledger/debug.log"
    assert "rollback_available" in pending

    handle = SESSIONS[session_id]
    assert "log_truncated" not in handle.session.transport.state  # still untouched

    decision = client.post(
        f"/sessions/{session_id}/remediations/{pending['id']}/decision",
        json={"approved": True, "approver": "operator", "reason": "log is discardable"},
    )
    assert decision.status_code == 200

    report = _await_report(client, session_id)
    assert report["summary"]
    assert report["findings"]

    journal = client.get(f"/sessions/{session_id}/journal").json()
    assert journal["intact"] is True
    kinds = [e["kind"] for e in journal["entries"]]
    assert "approval_decision" in kinds and "remediation_applied" in kinds

    assert client.delete(f"/sessions/{session_id}").json()["closed"] is True


def test_approving_an_unprotected_change_requires_the_acknowledgement(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    _patch_client(monkeypatch)
    # A target that can take no rollback point at all.
    original = api_module.faulty_workstation
    monkeypatch.setattr(
        api_module,
        "faulty_workstation",
        lambda **kw: original(snapshot_capable=False, **kw),
    )

    created = client.post(
        "/sessions",
        json={
            "authorization": "I own it",
            "asserted_by": "operator",
            "mock": True,
            "journal_path": str(tmp_path / "api2.sqlite"),
        },
    )
    session_id = created.json()["session_id"]
    handle = SESSIONS[session_id]

    # The plan is optimistic — it forecasts a file backup — so the first approval is
    # taken on that promise.
    pending = _await_pending(client, session_id)
    assert pending["rollback_available"] is True
    first = client.post(
        f"/sessions/{session_id}/remediations/{pending['id']}/decision",
        json={"approved": True, "approver": "operator"},
    )
    assert first.status_code == 200

    # The backup could not actually be taken, so the operator is asked again — this time
    # with the truth, and nothing has been applied in the meantime.
    pending = _await_pending(client, session_id)
    assert pending["rollback_available"] is False
    assert "log_truncated" not in handle.session.transport.state

    refused = client.post(
        f"/sessions/{session_id}/remediations/{pending['id']}/decision",
        json={"approved": True, "approver": "operator"},
    )
    assert refused.status_code == 409
    assert "acknowledge_no_rollback" in refused.json()["detail"]
    assert "log_truncated" not in handle.session.transport.state

    accepted = client.post(
        f"/sessions/{session_id}/remediations/{pending['id']}/decision",
        json={"approved": True, "approver": "operator", "acknowledge_no_rollback": True},
    )
    assert accepted.status_code == 200
    _await_report(client, session_id)
    assert "log_truncated" in handle.session.transport.state


def test_deciding_a_remediation_that_is_not_pending_is_a_404(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    _patch_client(monkeypatch)
    created = client.post(
        "/sessions",
        json={
            "authorization": "I own it",
            "asserted_by": "operator",
            "mock": True,
            "journal_path": str(tmp_path / "api3.sqlite"),
        },
    )
    session_id = created.json()["session_id"]
    _await_pending(client, session_id)

    response = client.post(
        f"/sessions/{session_id}/remediations/rem_nonexistent/decision",
        json={"approved": True},
    )
    assert response.status_code == 404


def test_unknown_sessions_are_404(client: TestClient) -> None:
    assert client.get("/sessions/sess_nope").status_code == 404
    assert client.get("/sessions/sess_nope/report").status_code == 404


# ------------------------------------------------------------------------------- helpers


def _await_pending(client: TestClient, session_id: str, tries: int = 200) -> dict:
    """Poll until the background loop parks on an approval."""
    for _ in range(tries):
        for item in client.get(f"/sessions/{session_id}/remediations").json():
            if item.get("awaiting_decision"):
                return item
        _tick()
    raise AssertionError("the session never asked for an approval")


def _await_report(client: TestClient, session_id: str, tries: int = 200) -> dict:
    for _ in range(tries):
        response = client.get(f"/sessions/{session_id}/report")
        if response.status_code == 200:
            return response.json()
        _tick()
    raise AssertionError("the session never produced a report")


def _tick() -> None:
    """Yield to the portal thread so the background session task can make progress."""
    time.sleep(0.01)
