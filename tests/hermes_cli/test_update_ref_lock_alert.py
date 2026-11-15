"""The same Git ref lock failing two distinct update runs opens ONE Kanban card (diagnostic only)."""
from __future__ import annotations

import builtins
import json
import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import update_receipt, update_ref_lock_alert as alert


@pytest.fixture
def board(_isolate_hermes_home, tmp_path, monkeypatch):
    home = tmp_path / "kanban_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    return home


def _cards():
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        return conn.execute(
            "SELECT id, idempotency_key, body FROM tasks WHERE created_by = ?", (alert.AUTHOR,)
        ).fetchall()


def _lock(tmp_path, name="HEAD.lock"):
    path = tmp_path / "repo" / ".git" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def _git_output(path):
    return f"fatal: cannot lock ref 'HEAD': Unable to create '{path}': File exists.\n"


def _failed_run(output):
    """One update run through the real receipt consumer: begin -> record -> finalize."""
    update_receipt.begin_update_receipt()
    update_receipt.record_ref_lock_identity(output)
    update_receipt.record_stop_reason(update_receipt.git_output_stop_class(output, ["git", "merge"]))
    path = update_receipt.finalize_update_receipt("failed")
    assert path is not None
    return path


def test_identity_is_canonical_path_plus_generation(tmp_path):
    lock = _lock(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(lock.parent)
    a = alert.lock_identity(_git_output(lock))
    b = alert.lock_identity(_git_output(link / "HEAD.lock"))
    assert a["path"] == b["path"] == os.path.realpath(lock)
    assert a["ino"] == lock.stat().st_ino and a["mtime_ns"] == lock.stat().st_mtime_ns
    assert alert.identity_key(a) == alert.identity_key(b)


def test_identity_ignores_non_ref_locks_and_other_errors(tmp_path):
    index = _lock(tmp_path, "index.lock")
    assert alert.lock_identity(_git_output(index)) is None
    assert alert.lock_identity("fatal: Unable to create '/x/.git/HEAD.lock': Permission denied") is None


def test_same_lock_two_distinct_runs_opens_exactly_one_card(board, tmp_path):
    lock = _lock(tmp_path)
    _failed_run(_git_output(lock))
    assert _cards() == []  # one run is not yet a pattern
    _failed_run(_git_output(lock))
    cards = _cards()
    assert len(cards) == 1
    assert cards[0][1].startswith(alert.KEY_PREFIX)
    assert str(os.path.realpath(lock)) in cards[0][2] and "nothing was deleted" in cards[0][2]
    assert lock.exists()  # a diagnostic never removes the lock
    _failed_run(_git_output(lock))  # third run: still one card
    assert len(_cards()) == 1


def test_duplicate_run_id_does_not_count_twice(board, tmp_path):
    lock = _lock(tmp_path)
    update_receipt.begin_update_receipt()
    update_receipt.record_ref_lock_identity(_git_output(lock))
    update_receipt.record_stop_reason("ref_lock_stale")
    receipt = update_receipt._current.get()
    assert receipt is not None
    data = dict(receipt.data, outcome="failed")
    for _ in range(3):  # the same run observed repeatedly
        alert.observe(data)
    ledger = json.loads((update_receipt._receipt_dir() / alert._LEDGER).read_text(encoding="utf-8"))
    assert next(iter(ledger["entries"].values()))["runs"] == [data["update_id"]]
    assert _cards() == []


@pytest.mark.parametrize("outcome", ["success", "running", "interrupted", None])
def test_only_failed_receipts_count_stale_stop_class(board, tmp_path, outcome):
    lock = _lock(tmp_path)
    _failed_run(_git_output(lock))
    update_receipt.begin_update_receipt()
    update_receipt.record_ref_lock_identity(_git_output(lock))
    update_receipt.record_stop_reason(alert.STOP_CLASS)
    receipt = update_receipt._current.get()
    assert receipt is not None
    data = dict(receipt.data, outcome=outcome)
    alert.observe(data)
    assert _cards() == []
    _failed_run(_git_output(lock))
    assert len(_cards()) == 1


def test_different_locks_do_not_combine(board, tmp_path):
    _failed_run(_git_output(_lock(tmp_path, "HEAD.lock")))
    _failed_run(_git_output(_lock(tmp_path, "packed-refs.lock")))
    assert _cards() == []


def test_new_generation_of_same_path_is_a_different_lock(board, tmp_path):
    lock = _lock(tmp_path)
    _failed_run(_git_output(lock))
    lock.unlink()
    _lock(tmp_path)
    os.utime(lock, ns=(1, 1))  # a fresh file at the same path, different mtime/inode
    _failed_run(_git_output(lock))
    assert _cards() == []


def test_publish_failure_is_retried_and_still_yields_one_card(board, tmp_path, monkeypatch):
    lock = _lock(tmp_path)
    calls = []

    def broken(entry, key):
        calls.append(key)
        raise RuntimeError("board locked")

    real = alert.publish_card
    monkeypatch.setattr(alert, "publish_card", broken)
    _failed_run(_git_output(lock))
    path = _failed_run(_git_output(lock))  # publication fails; the receipt is still written
    assert path.exists() and calls and _cards() == []
    monkeypatch.setattr(alert, "publish_card", real)
    # any later finalize retries the pending entry, even for an unrelated outcome
    update_receipt.begin_update_receipt()
    assert update_receipt.finalize_update_receipt("success") is not None
    assert len(_cards()) == 1
    update_receipt.begin_update_receipt()
    update_receipt.finalize_update_receipt("success")
    assert len(_cards()) == 1


def test_crash_between_card_and_ledger_does_not_duplicate(board, tmp_path):
    # create_task's idempotency key makes a republish after a lost ledger write return the same card.
    entry = {"identity": alert.lock_identity(_git_output(_lock(tmp_path))), "runs": ["a", "b"]}
    key = alert.identity_key(entry["identity"])
    assert alert.publish_card(entry, key) == alert.publish_card(entry, key)
    assert len(_cards()) == 1


def _data(run, path="/repo/.git/HEAD.lock"):
    return {"update_id": run, "outcome": "failed", "stop_class": alert.STOP_CLASS,
            alert.FACT: {"path": path, "dev": 1, "ino": 2, "mtime_ns": 3}}


def test_bom_ledger_keeps_prior_run(tmp_path):
    path = tmp_path / alert._LEDGER
    ident = _data("first")[alert.FACT]
    key = alert.identity_key(ident)
    ledger = {"entries": {key: {"identity": ident, "runs": ["first"], "seen_at": 1,
                               "card": None, "attempts": 0}}}
    path.write_text(json.dumps(ledger), encoding="utf-8-sig")
    published = []
    alert.observe(_data("second"), directory=tmp_path, now=2,
                  publisher=lambda entry, key: published.append(list(entry["runs"])) or "card")
    assert published == [["first", "second"]]


def test_unsupported_locking_never_observes_unlocked(tmp_path, monkeypatch, caplog):
    real_import = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    with caplog.at_level(logging.WARNING):
        assert alert.observe(_data("first"), directory=tmp_path) == []
    assert not (tmp_path / alert._LEDGER).exists()
    assert any(r.exc_info and "bookkeeping failed" in r.message for r in caplog.records)


def test_diagnostic_catchalls_keep_tracebacks(tmp_path, monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("diagnostic failure")

    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(alert, "lock_identity", broken)
        update_receipt.record_ref_lock_identity("ignored")
        alert.observe(_data("first"), directory=tmp_path, publisher=broken)
        alert.observe(_data("second"), directory=tmp_path, publisher=broken)
    records = [r for r in caplog.records if r.name in {alert.__name__, update_receipt.__name__}
               and ("identity not recorded" in r.message or "card not filed" in r.message)]
    assert len(records) == 2
    assert all(r.exc_info for r in records)


@pytest.mark.parametrize("site", ["merge_result", "called_process_error"])
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_update_cmd_failures_reach_real_alert_consumer(board, tmp_path, monkeypatch, site, stream):
    from hermes_cli import update_cmd

    lock = _lock(tmp_path)
    output = _git_output(lock)
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(_is_windows=lambda: False))
    monkeypatch.setattr(update_cmd, "_unrestored_autostash_notice", lambda: None)

    def git_run(cmd, args, **kwargs):
        assert args in [["merge", "--ff-only", "target"],
                        ["merge-base", "--is-ancestor", "HEAD", "target"]]
        return SimpleNamespace(returncode=128 if args[0] == "merge" else 0,
                               stderr=output if stream == "stderr" else "",
                               stdout=output if stream == "stdout" else "")

    monkeypatch.setattr(update_cmd, "_git_run", git_run)
    for run in range(3):
        update_receipt.begin_update_receipt()
        with pytest.raises(SystemExit) as error:
            if site == "merge_result":
                update_cmd._move_checkout_to(["git"], "main", "origin/main", "target", "old")
            else:
                exc = subprocess.CalledProcessError(
                    128, ["git", "fetch"], stderr=output.encode() if stream == "stderr" else None,
                    output=output.encode() if stream == "stdout" else None)
                update_cmd._handle_update_called_process_error(exc, object(), False, False)
        assert error.value.code == 1
        if site == "merge_result":
            path = update_receipt.finalize_pending_update_receipt(1, "sys.exit(1)")
            assert path is not None
        receipt = update_receipt.read_latest_receipt()
        assert receipt is not None
        assert receipt["outcome"] == "failed" and receipt["stop_class"] == alert.STOP_CLASS
        assert receipt[alert.FACT] == alert.lock_identity(output)
        assert len(_cards()) == (0 if run == 0 else 1)
        assert lock.exists()


def test_successful_finalize_with_stale_reason_does_not_count(board, tmp_path):
    lock = _lock(tmp_path)
    _failed_run(_git_output(lock))
    update_receipt.begin_update_receipt()
    update_receipt.record_ref_lock_identity(_git_output(lock))
    update_receipt.record_stop_reason(alert.STOP_CLASS)
    assert update_receipt.finalize_update_receipt("success") is not None
    assert _cards() == []


def test_ledger_is_bounded_by_entries_and_run_history(tmp_path):
    for run in range(alert._MAX_RUNS + 3):
        alert.observe(_data(str(run)), directory=tmp_path, now=run, publisher=lambda *_: "card")
    ledger = json.loads((tmp_path / alert._LEDGER).read_text(encoding="utf-8"))
    assert next(iter(ledger["entries"].values()))["runs"] == [
        str(run) for run in range(3, alert._MAX_RUNS + 3)]
    for run in range(alert._MAX_ENTRIES + 3):
        alert.observe(_data(str(run), f"/repo/.git/refs/heads/{run}.lock"),
                      directory=tmp_path, now=100 + run)
    ledger = json.loads((tmp_path / alert._LEDGER).read_text(encoding="utf-8"))
    assert len(ledger["entries"]) == alert._MAX_ENTRIES
    assert {e["identity"]["path"] for e in ledger["entries"].values()} == {
        f"/repo/.git/refs/heads/{run}.lock" for run in range(3, alert._MAX_ENTRIES + 3)}


def test_expired_observation_does_not_combine_with_new_run(tmp_path):
    alert.observe(_data("first"), directory=tmp_path, now=1)
    published = []
    alert.observe(_data("second"), directory=tmp_path, now=alert._MAX_AGE_S + 2,
                  publisher=lambda *args: published.append(args) or "card")
    assert published == []
    ledger = json.loads((tmp_path / alert._LEDGER).read_text(encoding="utf-8"))
    assert next(iter(ledger["entries"].values()))["runs"] == ["second"]


def test_lock_acquisition_failure_does_not_write_or_publish(tmp_path, monkeypatch, caplog):
    import fcntl

    def fail_lock(*args):
        raise OSError("cannot acquire guard")

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    with caplog.at_level(logging.WARNING):
        assert alert.observe(_data("first"), directory=tmp_path) == []
    assert not (tmp_path / alert._LEDGER).exists()
    assert any(r.exc_info and "bookkeeping failed" in r.message for r in caplog.records)
