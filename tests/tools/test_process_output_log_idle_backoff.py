"""The output-log tail backs off while its process is quiet and picks new output up on the next poll.

Card t_01a1d59ed9eb75fe (design t_01a1d563324d7e8e §6, serve residue): a fixed 0.2 s tail cost ~0.16% of a core
per idle reader on the hub. [daveey]
"""
from types import SimpleNamespace
import time

import tools.process_registry_output_log as olog
from tools.process_registry import ProcessRegistry


class _ScriptedFinish:
    """Stands in for ``session._reader_finish_requested``: records each wait and runs the test's hook."""

    def __init__(self, on_wait):
        self.waits: list[float] = []
        self._set = False
        self._on_wait = on_wait

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout=None) -> bool:
        self.waits.append(timeout)
        self._on_wait(len(self.waits))
        return self._set


def _reader(tmp_path, monkeypatch, on_wait, *, polls_alive):
    """Run ``_log_reader_loop`` on an adopted (no Popen) session whose process stays 'running' for
    ``polls_alive`` liveness checks. Waits go through the scripted finish event; a plain ``time.sleep`` in
    the loop is recorded too (it is what the pre-backoff reader called)."""
    log = tmp_path / "proc_quiet.log"
    log.write_bytes(b"")
    (tmp_path / "proc_quiet.exit").write_text("0\n")
    registry = ProcessRegistry()
    session = registry._new_session("c", "t", "t", "k", str(tmp_path), persist_on_release=True)
    session.output_log = str(log)
    session.process = None
    finish = _ScriptedFinish(lambda n: on_wait(n, log))
    session._reader_finish_requested = finish
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        on_wait(len(slept), log)

    monkeypatch.setattr(olog, "time", SimpleNamespace(sleep=sleep, time=time.time))
    checks = []

    def fate(pid, start):
        checks.append(1)
        return "running" if len(checks) <= polls_alive else "gone"

    monkeypatch.setattr(registry, "_detached_host_fate", fate)
    finished = []
    monkeypatch.setattr(registry, "_finish_exited", lambda s, code, **k: finished.append(code))
    ingested: list[tuple[int, str]] = []
    real_ingest = registry._ingest_output
    monkeypatch.setattr(registry, "_ingest_output",
                        lambda s, text: ingested.append((len(finish.waits) + len(slept), text)) or real_ingest(s, text))
    registry._log_reader_loop(session)
    return finish.waits + slept, ingested, finished, session


def test_quiet_log_backs_off_to_the_cap(tmp_path, monkeypatch):
    waits, _ingested, finished, _session = _reader(tmp_path, monkeypatch, lambda n, log: None, polls_alive=8)
    assert waits == [0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 2.0, 2.0]
    assert finished == [0]


def test_output_after_a_quiet_spell_is_picked_up_on_the_next_poll_and_resets_the_wait(tmp_path, monkeypatch):
    def on_wait(n, log):
        if n == 5:  # the reader is now waiting the full cap
            with open(log, "ab") as fh:
                fh.write(b"late line\n")

    waits, ingested, finished, session = _reader(tmp_path, monkeypatch, on_wait, polls_alive=8)
    # Read right after the 5th wait returned, before any further wait.
    assert ingested[0] == (5, "late line\n")
    assert waits[:5] == [0.2, 0.4, 0.8, 1.6, 2.0]
    assert waits[5:] == [0.2, 0.4]  # the wait starts short again after output
    assert session.output_buffer == "late line\n"
    assert finished == [0]


def test_a_finish_request_ends_the_tail(tmp_path, monkeypatch):
    """poll()/wait() reconcile sets ``_reader_finish_requested``; the idle wait is on that event, so the
    reader drains and returns."""
    import threading

    log = tmp_path / "proc_fin.log"
    log.write_bytes(b"")
    registry = ProcessRegistry()
    session = registry._new_session("c", "t", "t", "k", str(tmp_path), persist_on_release=True)
    session.output_log = str(log)
    session.process = None
    monkeypatch.setattr(registry, "_detached_host_fate", lambda pid, start: "running")
    done = threading.Event()
    monkeypatch.setattr(registry, "_finish_exited", lambda s, code, **k: done.set())
    reader = threading.Thread(target=registry._log_reader_loop, args=(session,), daemon=True)
    reader.start()
    from tests.waiting import wait_until
    wait_until(lambda: reader.is_alive())
    with open(log, "ab") as fh:
        fh.write(b"tail\n")
    wait_until(lambda: session.output_buffer == "tail\n", what="the written line")
    session._reader_finish_requested.set()
    reader.join(5)
    assert done.is_set()
    assert not reader.is_alive()


def test_a_finish_request_ends_a_long_idle_wait_at_once(tmp_path, monkeypatch):
    """The backed-off wait is on ``_reader_finish_requested``, not a sleep: with a 30 s wait in progress, a
    poll()/wait() reconcile's finish request still ends the reader within seconds."""
    import threading

    monkeypatch.setattr(olog, "_TAIL_SECONDS", 30.0)
    monkeypatch.setattr(olog, "_IDLE_TAIL_MAX_SECONDS", 30.0)
    log = tmp_path / "proc_long.log"
    log.write_bytes(b"tail\n")  # read before the first (30 s) wait
    registry = ProcessRegistry()
    session = registry._new_session("c", "t", "t", "k", str(tmp_path), persist_on_release=True)
    session.output_log = str(log)
    session.process = None
    monkeypatch.setattr(registry, "_detached_host_fate", lambda pid, start: "running")
    done = threading.Event()
    monkeypatch.setattr(registry, "_finish_exited", lambda s, code, **k: done.set())
    reader = threading.Thread(target=registry._log_reader_loop, args=(session,), daemon=True)
    reader.start()
    from tests.waiting import wait_until
    wait_until(lambda: session.output_buffer == "tail\n", what="the line written before the start")
    session._reader_finish_requested.set()
    reader.join(5)
    assert not reader.is_alive(), "the finish request did not end the 30 s idle wait"
    assert done.is_set()
