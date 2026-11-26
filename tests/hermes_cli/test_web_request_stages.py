"""Request-stage evidence is complete-or-explicitly-gapped, never causal by absence."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from hermes_cli import web_request_stages as stages

class Sink:
    def __init__(self):
        self.rows = []

    def emit(self, stage, **fields):
        self.rows.append({"stage": stage, **fields})


def scope(path="/api/plugins/user-steps/requests", headers=()):
    return {"type": "http", "path": path, "method": "GET", "headers": headers,
            "query_string": b"token=DO_NOT_LOG", "client": ("private", 1)}


@pytest.mark.asyncio
@pytest.mark.parametrize('selected_path', ['/api/plugins/user-steps/requests', '/api/plugins/session-mentions/result'])
async def test_full_asgi_order_bytes_and_sanitization(selected_path):
    sink = Sink()
    rid = b"a" * 32

    async def endpoint(s, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"secret", b"DO_NOT_LOG")]})
        await send({"type": "http.response.body", "body": b"secret", "more_body": True})
        await send({"type": "http.response.body", "body": b"xyz"})

    async def gate(s, receive, send):
        assert sink.rows[0]["stage"] == "ingress"
        await stages.RequestStageHandler(endpoint)(s, receive, send)

    messages = []

    async def send(message):
        messages.append(message)

    request_scope = scope(path=selected_path, headers=[(b"x-hermes-req-id", rid)])
    request_scope["method"] = "POST"
    await stages.RequestStageIngress(gate, sink)(request_scope, None, send)
    assert [r["stage"] for r in sink.rows] == [
        "ingress", "handler_dispatch", "response_start", "response_start_sent",
        "response_body", "response_body", "response_end", "app_exit",
    ]
    assert all(r["id"] == rid.decode() and r["client_id"] and r["method"] == "POST" for r in sink.rows)
    assert sink.rows[-2]["bytes"] == 9
    assert "DO_NOT_LOG" not in json.dumps(sink.rows)
    assert "private" not in json.dumps(sink.rows)
    assert messages[-1]["body"] == b"xyz"


@pytest.mark.asyncio
async def test_send_backpressure_is_not_recorded_as_completed():
    sink = Sink()
    started, release = asyncio.Event(), asyncio.Event()

    async def endpoint(s, receive, send):
        await send({"type": "http.response.body", "body": b"x" * 20})

    async def send(message):
        started.set()
        await release.wait()

    task = asyncio.create_task(stages.RequestStageIngress(endpoint, sink)(scope(), None, send))
    await asyncio.wait_for(started.wait(), 2)
    assert sink.rows[-1]["stage"] == "response_body"
    assert sink.rows[-1]["bytes"] == 20
    assert not any(r["stage"] == "response_end" for r in sink.rows)
    release.set()
    await asyncio.wait_for(task, 2)
    assert sink.rows[-2]["stage"] == "response_end"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [[], [(b"x-hermes-req-id", b"secret\n" * 100)],
                                       [(b"x-hermes-req-id", b"a" * 32)] * 2])
async def test_legacy_and_invalid_ids_get_local_id_without_echo(headers):
    sink = Sink()

    async def endpoint(s, receive, send):
        raise ValueError("private error")

    with pytest.raises(ValueError):
        await stages.RequestStageIngress(endpoint, sink)(scope(headers=headers), None, None)
    assert [r["stage"] for r in sink.rows] == ["ingress", "app_exit"]
    assert len(sink.rows[0]["id"]) == 32
    assert not sink.rows[0]["client_id"]
    assert "secret" not in json.dumps(sink.rows)
    assert "private error" not in json.dumps(sink.rows)


@pytest.mark.asyncio
async def test_unselected_and_websocket_bypass_journal():
    sink = Sink()
    seen = []

    async def endpoint(s, receive, send):
        seen.append(s)

    observer = stages.RequestStageIngress(endpoint, sink)
    await observer(scope("/api/private/secret"), None, None)
    ws = scope()
    ws["type"] = "websocket"
    await observer(ws, None, None)
    assert len(seen) == 2
    assert sink.rows == []


def test_bounded_overflow_gaps_and_io_loss(tmp_path, monkeypatch):
    # Drive the real consumer deterministically after filling the ring.
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    journal = stages.StageJournal(tmp_path / "stages.jsonl", capacity=2)
    journal.emit("one")
    journal.emit("two")
    journal.emit("dropped")
    assert journal.dropped == 1
    journal.stopped.set()
    journal._run()
    journal.emit("coverage")
    journal._run()
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert [r["seq"] for r in rows] == [1, 2, 4]
    assert rows[-1]["dropped"] == 1
    bad = stages.StageJournal(tmp_path / "stages.jsonl" / "invalid", capacity=2)
    bad.emit("lost")
    bad.stopped.set()
    bad._run()
    assert bad.io_lost == 1


def test_io_never_runs_on_producer(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = stages.logging.handlers.RotatingFileHandler.shouldRollover

    def blocked(handler, record):
        entered.set()
        assert release.wait(3)
        return original(handler, record)

    monkeypatch.setattr(stages.logging.handlers.RotatingFileHandler, "shouldRollover", blocked)
    journal = stages.StageJournal(tmp_path / "stages.jsonl", capacity=2)
    try:
        journal.emit("one")
        assert entered.wait(2)
        journal.emit("two")
        journal.emit("three")
        journal.emit("drop")
        assert journal.dropped == 1
    finally:
        journal.stopped.set()
        release.set()
        journal.thread.join(3)
    assert not journal.thread.is_alive()


def test_real_app_registers_observers_outside_gates():
    # Importing facade here exercises production registration, not an invented onion.
    from hermes_cli.web_server import app
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is stages.RequestStageIngress
    assert classes[-1] is stages.RequestStageHandler


@pytest.mark.asyncio
async def test_production_middleware_chain_preserves_shared_id_and_payload(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from hermes_cli import web_server

    sink = Sink()
    monkeypatch.setattr(stages, "journal", lambda: sink)
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    application = FastAPI(middleware=web_server.app.user_middleware)

    @application.get("/api/health")
    async def health():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1") as client:
        response = await client.get("/api/health", headers={"X-Hermes-Req-Id": "b" * 32})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    names = [r["stage"] for r in sink.rows]
    assert names[0] == "ingress"
    assert names.index("handler_dispatch") < names.index("response_start")
    assert names[-1] == "app_exit"
    assert all(r["id"] == "b" * 32 for r in sink.rows)
    end = next(r for r in sink.rows if r["stage"] == "response_end")
    assert end["bytes"] == len(response.content)


@pytest.mark.asyncio
async def test_result_auth_refusal_retains_correlated_ingress(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from hermes_cli import web_server

    sink = Sink()
    monkeypatch.setattr(stages, "journal", lambda: sink)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "test-stage-token")
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    application = FastAPI(middleware=web_server.app.user_middleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1") as client:
        response = await client.post("/api/plugins/session-mentions/result?token=DO_NOT_LOG",
                                     json={"private": "DO_NOT_LOG"}, headers={"X-Hermes-Req-Id": "b" * 32})
    assert response.status_code == 401
    assert sink.rows[0]["stage"] == "ingress"
    assert all(r["id"] == "b" * 32 and r["client_id"] and r["method"] == "POST" for r in sink.rows)
    assert "handler_dispatch" not in [r["stage"] for r in sink.rows]
    assert next(r for r in sink.rows if r["stage"] == "response_start")["status"] == 401
    assert "DO_NOT_LOG" not in json.dumps(sink.rows)


def test_rotation_retains_exactly_four_bounded_files(tmp_path, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    journal = stages.StageJournal(tmp_path / "capture" / "stages.jsonl", max_bytes=600)
    for _ in range(40):
        journal.emit("coverage")
    journal.stopped.set()
    journal._run()
    files = list(journal.path.parent.iterdir())
    assert len(files) == 4
    assert all(p.stat().st_size < 600 for p in files)
    latest = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert latest[-1]["seq"] == 40


def test_idle_coverage_is_persisted_without_requests(tmp_path):
    journal = stages.StageJournal(tmp_path / "stages.jsonl")
    # Observe actual writer progress with a bounded Event, not a timing sleep.
    seen = threading.Event()
    original = journal.emit

    def observed(stage, **fields):
        original(stage, **fields)
        if stage == "coverage":
            seen.set()

    journal.emit = observed
    try:
        assert seen.wait(3)
    finally:
        journal.stopped.set()
        journal.thread.join(3)
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert rows[0]["stage"] == "coverage"
    assert rows[0]["dropped"] == rows[0]["io_lost"] == 0
