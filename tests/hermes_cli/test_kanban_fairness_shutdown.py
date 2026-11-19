"""Deterministic shutdown admission and process-budget regressions."""
import threading
import os

from hermes_cli import kanban_fairness_runtime as runtime


def activate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return runtime.activate({"kanban": {"fairness_capture": {"enabled": True, "capture_id": "test"}}},
                            home=tmp_path, board="default", source_gateway="gateway", board_uid="board",
                            clock_error_ns=0)


def test_rejected_producer_paused_before_loss_cannot_certify(tmp_path, monkeypatch):
    session = activate(tmp_path, monkeypatch)
    assert session is not None
    payload = session.loss_payload("redaction", 1)
    assert session.enqueue("loss", payload)
    entered, release = threading.Event(), threading.Event()
    actual = session.note_loss

    def paused(reason):
        entered.set()
        assert release.wait(5)
        actual(reason)

    monkeypatch.setattr(session, "note_loss", paused)
    with session.lock:
        producer = threading.Thread(target=lambda: session.enqueue("loss", payload))
        producer.start()
        assert entered.wait(2)
    try:
        assert not session.close()
        assert not session.sealed
    finally:
        release.set()
        producer.join(3)
        session.thread.join(3)


def test_process_shutdown_shares_one_deadline(monkeypatch):
    now = [10.0]
    budgets = []
    begun = []

    class Fake:
        def begin_close(self, timeout):
            begun.append(self)

        def close(self, timeout=2):
            assert len(begun) == 3
            budgets.append(timeout)
            now[0] += timeout

    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runtime, "_SESSIONS", {(os.getpid(), name): Fake() for name in "abc"})
    runtime.close_all()
    assert budgets == [2.0, 0.0, 0.0]


def test_shutdown_ignores_fork_inherited_sessions(monkeypatch):
    monkeypatch.setattr(runtime, "_SESSIONS", {(os.getpid() + 1, "parent"): object()})
    runtime.close_all()


def test_slow_receipt_publication_returns_authorization_not_false_timeout(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_tail as tail

    session = activate(tmp_path, monkeypatch)
    assert session is not None
    assert session.enqueue("loss", session.loss_payload("redaction", 1))
    entered, release = threading.Event(), threading.Event()
    actual = tail.close_epoch
    results = []

    def held(*args):
        entered.set()
        assert release.wait(5)
        return actual(*args)

    monkeypatch.setattr(tail, "close_epoch", held)
    closer = threading.Thread(target=lambda: results.append(session.close(timeout=0.25)))
    closer.start()
    try:
        assert entered.wait(2)
        closer.join(2)
        assert not closer.is_alive()
        # True authorizes publication of data sealed within deadline; only the
        # durable receipt proves publication. An authorized tail is never revoked.
        assert results == [True]
        assert not (session.out / tail.CLOSE).exists()
    finally:
        release.set()
        closer.join(3)
        session.thread.join(3)
    assert session.sealed


def test_drain_batches_records_with_bounded_writer_calls(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual = runtime.append_records
    sizes = []

    def held(out, records, **kwargs):
        sizes.append(len(records))
        if len(sizes) == 1:
            entered.set()
            assert release.wait(5)
        return actual(out, records, **kwargs)

    monkeypatch.setattr(runtime, "append_records", held)
    session = activate(tmp_path, monkeypatch)
    assert session is not None
    payload = session.loss_payload("redaction", 1)
    assert session.enqueue("loss", payload)
    try:
        assert entered.wait(2)
        assert all(session.enqueue("loss", payload) for _ in range(300))
    finally:
        release.set()
        assert session.close()
    assert sum(sizes) == 301
    assert max(sizes) <= runtime.MAX_BATCH_RECORDS
    assert len(sizes) == 4
