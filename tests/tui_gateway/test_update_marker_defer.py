"""Only incarnation-bound same-install update custody suppresses an automatic card."""
import itertools
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import active_sessions, process_identity, update_lock
from tests.tui_gateway import test_automatic_ledger_defer as ledger_tests
from tui_gateway import server, turn_marker, update_marker_owner

admission = ledger_tests.admission


@pytest.fixture
def custody(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("positive checkout contention proof is POSIX-only")
    home, install = tmp_path / "home", tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(process_identity, "_ledger_path", lambda: tmp_path / "spawn-ledger.json")
    assert process_identity.register_self("serve", project_root=install)
    pid, birth = os.getpid(), active_sessions._own_start_time()
    assert birth is not None
    assert turn_marker.record_turn_start(home, "chat", "pending")
    assert turn_marker.mark_turn_paused(home, "chat", "core_update_pause")
    refusal = active_sessions.ActiveSessionRefusal("owned", active_sessions.SESSION_NOT_OWNED)
    refusal.holder_pid, refusal.holder_start_time = pid, birth
    refusal.ownership_cause = "lease_foreign"
    lock = update_lock.UpdateLock(path=home / update_lock.MARKER_NAME, install_root=install)
    assert lock.acquire()
    try:
        yield SimpleNamespace(home=home, install=install, refusal=refusal, lock=lock,
                              classify=lambda: update_marker_owner.classify_refusal(
                                  home, "chat", refusal, install_root=install))
    finally:
        lock.release()


def test_proven_marker_refusal_is_classified(custody):
    result = custody.classify()
    assert result.ownership_cause == "update_marker"
    assert result.holder_pid == os.getpid()
    assert result is not custody.refusal
    assert custody.refusal.ownership_cause == "lease_foreign"


def test_registry_producer_classifies_proven_update(custody, monkeypatch):
    original = update_marker_owner.classify_refusal
    monkeypatch.setattr(update_marker_owner, "classify_refusal",
                        lambda h, k, r: original(h, k, r, install_root=custody.install))
    monkeypatch.setattr(server, "_canonical_ledger_refusal", lambda s: None)
    monkeypatch.setattr(server, "_durable_owner_refusal", lambda s: None)
    # Real registry refusal, not a pre-labelled mock: two live records in this process.
    lease, refused = active_sessions.try_acquire_active_session(
        session_id="chat", surface="desktop", registry_home=custody.home, config={},
        metadata={"live_session_id": "first"})
    assert refused is None
    try:
        monkeypatch.setattr(server, "_load_cfg", dict)
        session = {"session_key": "chat", "profile_home": custody.home, "source": "desktop"}
        actual = server._ensure_active_session_slot("second", session, allow_takeover=False)
        assert actual.ownership_cause == "update_marker"
    finally:
        lease.release()


def test_automatic_update_marker_refusal_defers(admission, caplog):
    admission.session["_synthesized_turn"] = True
    admission.refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ownership_cause="update_marker", holder_pid=123)
    with caplog.at_level("INFO"):
        assert ledger_tests._admit(admission) is None
    assert admission.errors == []
    assert admission.starts == []
    assert admission.session["_turn_owner_refusal"] == "SESSION_NOT_OWNED"
    assert "update marker writer pid 123" in caplog.text


def test_update_deferral_retries_and_recovers(custody, admission, monkeypatch):
    admission.refusal = custody.classify()
    def submit(*args, **kwargs):
        return ledger_tests._admit(admission) is not None
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    for _ in range(3):
        assert not server._notif_run_submit("rid", "live", admission.session, "notice")
        assert server._notif_owner_refused(admission.session)
        admission.now = admission.session["_notif_owner_refused_until"]
    assert admission.errors == []
    admission.refusal = None
    assert server._notif_run_submit("rid", "live", admission.session, "notice")
    assert not server._notif_owner_refused(admission.session)


@pytest.mark.parametrize("synthesized,reason,cause", itertools.product(
    [True, False], ["SESSION_NOT_OWNED", "SESSION_COORDINATION_UNAVAILABLE"],
    ["update_marker", "lease_foreign", "unknown", None]))
def test_other_refusals_keep_their_card(admission, synthesized, reason, cause):
    if synthesized and reason == "SESSION_NOT_OWNED" and cause == "update_marker":
        return  # the sole positive shape, independently exercised above
    admission.session["_synthesized_turn"] = synthesized
    admission.refusal = SimpleNamespace(reason=reason, ownership_cause=cause, holder_pid=123)
    assert ledger_tests._admit(admission) is None
    assert len(admission.errors) == 1


@pytest.mark.parametrize("defect", ["no_marker", "not_paused", "different_writer", "stale_writer",
                                    "missing_birth", "foreign_install", "no_ledger", "stale_ledger",
                                    "no_update", "no_checkout_lock", "stale_updater", "legacy_updater",
                                    "unreadable_lock", "unknown_writer", "marker_changed"])
def test_unproven_marker_never_relabels(custody, monkeypatch, defect):
    marker = turn_marker.read_turn_marker(custody.home, "chat")
    path = custody.home / update_lock.MARKER_NAME

    def stale_ledger():
        from utils import atomic_json_write
        rows = process_identity._read_ledger(process_identity._ledger_path())
        rows[0]["create_time"] += 1
        atomic_json_write(process_identity._ledger_path(), rows)

    def update_birth(birth):
        lines = path.read_text().splitlines()[:2]
        path.write_text("\n".join(lines + ([] if birth is None else [f"ct:{birth:.3f}"])) + "\n")

    def denied(*args, **kwargs):
        raise PermissionError("fixture lock unreadable")

    reads = iter([marker, {**marker, "prompt": "successor"}])
    actions = {
        "no_marker": lambda: turn_marker.clear_turn_marker(custody.home, "chat"),
        "not_paused": lambda: turn_marker.record_turn_start(custody.home, "chat", "not paused"),
        "different_writer": lambda: setattr(custody.refusal, "holder_pid", os.getpid() + 1),
        "stale_writer": lambda: setattr(custody.refusal, "holder_start_time", active_sessions._own_start_time() + 1),
        "missing_birth": lambda: delattr(custody.refusal, "holder_start_time"),
        "foreign_install": lambda: process_identity.register_self("serve", project_root=custody.install / "foreign"),
        "no_ledger": lambda: process_identity._ledger_path().unlink(),
        "stale_ledger": stale_ledger,
        "no_update": path.unlink,
        "no_checkout_lock": update_lock._release_checkout,
        "stale_updater": lambda: update_birth(active_sessions._own_start_time() + 10),
        "legacy_updater": lambda: update_birth(None),
        "unreadable_lock": lambda: monkeypatch.setattr(update_marker_owner.os, "open", denied),
        "unknown_writer": lambda: monkeypatch.setattr(update_marker_owner, "marker_writer_state", lambda _: "unknown"),
        "marker_changed": lambda: monkeypatch.setattr(update_marker_owner, "read_turn_marker", lambda *a: next(reads)),
    }
    actions[defect]()
    assert custody.classify() is custody.refusal
    assert custody.refusal.ownership_cause == "lease_foreign"


_CHILD_WRITER = """
import sys
from pathlib import Path
from hermes_cli import active_sessions, process_identity, update_lock
from tui_gateway import turn_marker
home, install, ledger, ready = map(Path, sys.argv[1:])
process_identity._ledger_path = lambda: ledger
assert process_identity.register_self("serve", project_root=install)
lease, refused = active_sessions.try_acquire_active_session(
    session_id="chat", surface="desktop", registry_home=home, config={},
    metadata={"live_session_id": "child"})
assert refused is None
assert turn_marker.record_turn_start(home, "chat", "pending")
assert turn_marker.mark_turn_paused(home, "chat", "core_update_pause")
lock = update_lock.UpdateLock(path=home / update_lock.MARKER_NAME, install_root=install)
assert lock.acquire()
try:
    ready.write_text("ready")
    sys.stdin.read()
finally:
    lock.release()
    lease.release()
"""


def test_real_other_process_marker_update_defers(tmp_path, monkeypatch, admission):
    from tests.waiting import wait_until
    if os.name != "posix":
        pytest.skip("POSIX kernel contention proof")
    home, install = tmp_path / "home", tmp_path / "install"
    install.mkdir()
    ledger, ready = tmp_path / "ledger.json", tmp_path / "ready"
    monkeypatch.setattr(process_identity, "_ledger_path", lambda: ledger)
    proc = subprocess.Popen([sys.executable, "-c", _CHILD_WRITER, str(home), str(install), str(ledger), str(ready)],
                            cwd=Path.cwd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        wait_until(lambda: ready.exists() or proc.poll() is not None, what="child writer ready")
        assert proc.poll() is None, proc.communicate(timeout=10)
        _, refusal = active_sessions.try_acquire_active_session(
            session_id="chat", surface="desktop", registry_home=home, config={},
            metadata={"live_session_id": "parent"})
        assert refusal.holder_pid == proc.pid
        classified = update_marker_owner.classify_refusal(home, "chat", refusal, install_root=install)
        assert classified.ownership_cause == "update_marker"
        admission.refusal = classified
        admission.session["_synthesized_turn"] = True
        assert ledger_tests._admit(admission) is None
        assert admission.errors == []
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, (out, err)
        # Same historical marker after owner exit is NOT current update provenance.
        assert update_marker_owner.classify_refusal(home, "chat", refusal, install_root=install) is refusal
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


def test_registry_refusal_carries_original_holder_birth(tmp_path):
    first, refusal = active_sessions.try_acquire_active_session(
        session_id="marker-chat", surface="desktop", registry_home=tmp_path, config={},
        metadata={"live_session_id": "first"})
    assert refusal is None
    try:
        second, refusal = active_sessions.try_acquire_active_session(
            session_id="marker-chat", surface="desktop", registry_home=tmp_path, config={},
            metadata={"live_session_id": "second"})
        assert second is None
        assert refusal.holder_pid == os.getpid()
        assert refusal.holder_start_time == active_sessions._own_start_time()
        assert refusal.ownership_cause == "lease_foreign"
    finally:
        first.release()
