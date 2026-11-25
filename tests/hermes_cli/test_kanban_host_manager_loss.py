"""A root-attested host user-manager SIGKILL waives one owner-death repeat, never more. [daveey]

2026-11-12 02:34Z and 03:03Z, hermes-worker-1: ``user@1000.service`` was SIGKILLed twice and every card
in flight both times was charged by the ``owner_death_repeat`` guard (t_01a1cc39958aa6f9,
t_01a1ccf95e85a6f1, t_01a1cca130e69774). The worker's root recovery watchdog now attests the manager
loss with the exact gateway pid/birth it saw descend from that manager; only such evidence waives.
"""

import functools
import json
import os
import uuid

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_manager_loss as hml
from tests.hermes_cli.test_kanban_owner_death import (
    DEAD_OWNER, _claim, _kinds, _last_payload, _ledger, _reaped, _task)
from tests.hermes_cli.test_kanban_owner_death import board as owner_board  # fixture, registered by this name

_FIXTURES = (owner_board,)  # used here, so the test parameters below do not read as shadowing it
BOOT, HOST, BIRTH = "boot-a", "hermes-worker-1", 1432.33
NOW = 5000.0


def _incident(owner_pid=DEAD_OWNER, birth=BIRTH, **over):
    loss = dict(schema=1, boot_id=BOOT, host=HOST, observed_monotonic=4000.0, exit_monotonic=4010.0,
                exit_code="killed", exit_status=9, unit="user@1000.service", owner_unit="hermes-gateway.service",
                manager=dict(pid=900, ticks=100, invocation="a" * 32),
                owner=dict(pid=owner_pid, ticks=int(birth * 100), birth=birth))
    loss.update(over.pop("loss", {}))
    data = dict(incident_id=str(uuid.uuid4()), machine=HOST, status="recovered", unit="user@1000.service",
                down_at=1.0, observed_at=2.0, manager_loss=loss)
    data.update(over)
    return data


def _root(tmp_path, *records, name="status.json"):
    root = tmp_path / "recovery"
    (root / "incidents").mkdir(parents=True, exist_ok=True)
    for path in (root, root / "incidents"):
        path.chmod(0o755)
    for record in records:
        target = root / name if name == "status.json" else root / "incidents" / f"{record['incident_id']}.json"
        target.write_text(json.dumps(record))
        target.chmod(0o644)
    return root


def _check(root, receipt=None, **kw):
    kw = dict(root=root, uid=os.getuid(), boot_id=BOOT, host=HOST, now=NOW, check_ancestors=False) | kw
    receipt = {"pid": DEAD_OWNER, "birth": BIRTH} if receipt is None else receipt
    return hml.manager_loss_incident(receipt, **kw)


def test_an_exact_root_receipt_names_its_incident(tmp_path):
    record = _incident()
    assert _check(_root(tmp_path, record)) == record["incident_id"]
    queued = _incident()
    assert _check(_root(tmp_path / "q", queued, name="incidents")) == queued["incident_id"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(unit="hermes-gateway.service"),          # gateway-only incident
    lambda d: d.pop("manager_loss"),                            # legacy receipt
    lambda d: d.update(machine="hermes-worker-2"),
    lambda d: d["manager_loss"].update(boot_id="boot-b"),      # previous boot
    lambda d: d["manager_loss"].update(host="other"),
    lambda d: d["manager_loss"].update(schema=2),
    lambda d: d["manager_loss"].update(exit_status=15),         # not SIGKILL
    lambda d: d["manager_loss"].update(exit_code="exited"),
    lambda d: d["manager_loss"]["owner"].update(pid=DEAD_OWNER + 1),   # another gateway
    lambda d: d["manager_loss"]["owner"].update(pid=float(DEAD_OWNER)),
    lambda d: d["manager_loss"]["owner"].update(birth=BIRTH + 0.01),   # recycled pid
    lambda d: d["manager_loss"].update(observed_monotonic=3900.0),     # snapshot older than 60s
    lambda d: d["manager_loss"].update(exit_monotonic=NOW + 1),        # exit in the future
    lambda d: d["manager_loss"]["owner"].update(birth=4005.0),         # owner born after the snapshot
    lambda d: d["manager_loss"].update(exit_status=True),
    lambda d: d.update(incident_id=None),
])
def test_anything_short_of_an_exact_match_fails_closed(tmp_path, mutate):
    record = _incident()
    mutate(record)
    receipt = {"pid": DEAD_OWNER, "birth": BIRTH}
    if record.get("manager_loss", {}).get("owner", {}).get("birth") == 4005.0:
        receipt["birth"] = 4005.0  # the admitted owner itself, born after the snapshot
    assert _check(_root(tmp_path, record), receipt) is None


@pytest.mark.parametrize("receipt", [{}, {"pid": DEAD_OWNER}, {"pid": True, "birth": BIRTH},
                                     {"pid": 1, "birth": BIRTH}, {"pid": DEAD_OWNER, "birth": "1432.33"}])
def test_an_unusable_admission_receipt_fails_closed(tmp_path, receipt):
    assert _check(_root(tmp_path, _incident()), receipt) is None


def test_untrusted_files_fail_closed(tmp_path):
    record = _incident()
    root = _root(tmp_path, record)
    assert _check(root, uid=os.getuid() + 1) is None             # not owned by the trusted (root) uid
    (root / "status.json").chmod(0o666)
    assert _check(root) is None                                  # writable by others
    (root / "status.json").chmod(0o644)
    root.chmod(0o775)
    assert _check(root) is None                                  # group-writable directory
    root.chmod(0o755)
    (root / "status.json").write_text(json.dumps(record) + " " * hml.MAX_RECEIPT_BYTES)
    assert _check(root) is None                                  # oversized
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(record))
    (root / "status.json").unlink()
    (root / "status.json").symlink_to(target)
    assert _check(root) is None                                  # symlink
    (root / "status.json").unlink()
    (root / "status.json").write_text("{not json")
    assert _check(root) is None


def test_ancestors_must_not_be_writable_by_other_users(tmp_path):
    parent = tmp_path / "var"
    root = parent / "recovery"
    root.mkdir(parents=True)
    root.chmod(0o755)
    parent.chmod(0o777)
    assert not hml.trusted_tree(root, uid=os.getuid())
    assert not hml.trusted_tree(root)  # the default trusted uid is root; a test tree is never root-owned


def test_production_defaults_ignore_a_missing_watchdog_dir(tmp_path):
    assert hml.manager_loss_incident({"pid": DEAD_OWNER, "birth": BIRTH}, root=tmp_path / "absent") is None


_ORIGINAL = hml.manager_loss_incident


def _attested(monkeypatch, tmp_path, owner_pid):
    root = _root(tmp_path / str(owner_pid), _incident(owner_pid=owner_pid))
    monkeypatch.setattr(hml, "manager_loss_incident", functools.partial(
        _ORIGINAL, root=root, uid=os.getuid(), boot_id=BOOT, host=HOST, now=NOW, check_ancestors=False))


def test_a_repeat_at_an_attested_manager_loss_is_waived_once_then_charged(isolate_usage_accounting, owner_board, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    owners = [DEAD_OWNER, 50001, 50002]
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        for n, owner in enumerate(owners):
            _ledger(owner_board, owner)
            _attested(monkeypatch, tmp_path, owner)
            pid = 88001 + n
            _claim(conn, tid, pid, owner_board, owner_pid=owner, birth=BIRTH)
            _reaped(monkeypatch, [pid])
            crashed = kbd.detect_crashed_workers(conn)
            if n < 2:
                # 1st: an ordinary first owner death. 2nd: a repeat, waived by the root receipt.
                assert crashed == [] and _task(conn, tid).consecutive_failures == 0
            else:
                # 3rd: the card already used its waiver; a card that keeps killing its host stops.
                assert crashed == [tid] and _task(conn, tid).consecutive_failures == 1
        waived = _last_payload(conn, tid, "owner_restart_lost")
        assert waived["owner_death_pid"] == owners[1] and waived["owner_death_repeat_waived"] == DEAD_OWNER
        assert waived["host_manager_loss"] and waived["infrastructure"] is True
        charged = _last_payload(conn, tid, "crashed")
        assert charged["owner_death_repeat"] == owners[1] and "host_manager_loss" not in charged
        assert "gave_up" not in _kinds(conn, tid)


def test_a_repeat_without_root_evidence_is_still_charged(isolate_usage_accounting, owner_board, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        for n, owner in enumerate([DEAD_OWNER, 50001]):
            _ledger(owner_board, owner)
            # Evidence names a DIFFERENT gateway: an unrelated manager loss on this host.
            _attested(monkeypatch, tmp_path, owner + 7)
            _claim(conn, tid, 89001 + n, owner_board, owner_pid=owner, birth=BIRTH)
            _reaped(monkeypatch, [89001 + n])
            kbd.detect_crashed_workers(conn)
        assert _task(conn, tid).consecutive_failures == 1
        assert _last_payload(conn, tid, "crashed")["owner_death_repeat"] == DEAD_OWNER


def _run_metadata(conn, tid):
    return [json.loads(r[0]) if r[0] else {} for r in conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id", (tid,))]


def test_a_first_owner_death_never_spends_the_waiver(owner_board, monkeypatch, tmp_path):
    """Root evidence at a NON-repeat death is not consulted: the run is requeued as an ordinary owner
    death and its metadata carries no ``host_manager_loss``, so the card keeps its one waiver."""
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        _ledger(owner_board, DEAD_OWNER)
        _attested(monkeypatch, tmp_path, DEAD_OWNER)
        _claim(conn, tid, 87001, owner_board, owner_pid=DEAD_OWNER, birth=BIRTH)
        _reaped(monkeypatch, [87001])
        assert kbd.detect_crashed_workers(conn) == [] and _task(conn, tid).consecutive_failures == 0
        lost = _last_payload(conn, tid, "owner_restart_lost")
        assert lost["owner_death_pid"] == DEAD_OWNER and "host_manager_loss" not in lost
        assert "owner_death_repeat_waived" not in lost
        metadata = _run_metadata(conn, tid)
        assert len(metadata) == 1 and "host_manager_loss" not in metadata[0]


def test_a_huge_receipt_number_fails_closed(tmp_path):
    """``float(10**400)`` raises OverflowError: an absurd JSON int is no match, not an exception."""
    for n, loss in enumerate([dict(observed_monotonic=10 ** 400), dict(exit_monotonic=-(10 ** 400))]):
        assert _check(_root(tmp_path / f"loss{n}", _incident(loss=loss))) is None
    huge_owner = _incident()
    huge_owner["manager_loss"]["owner"]["birth"] = 10 ** 400
    assert _check(_root(tmp_path / "owner", huge_owner)) is None
    assert _check(_root(tmp_path / "receipt", _incident()), {"pid": DEAD_OWNER, "birth": 10 ** 400}) is None


def test_a_repeat_whose_receipt_breaks_the_parser_is_charged_without_breaking_the_sweep(
        isolate_usage_accounting,
        owner_board, monkeypatch, tmp_path):
    """Any error reading the root evidence fails closed: the repeat is charged as before and the
    dead-worker sweep carries on (2026-11-12 review of PR #1231: OverflowError escaped ``note()``)."""
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        for n, owner in enumerate([DEAD_OWNER, 50001]):
            _ledger(owner_board, owner)
            root = _root(tmp_path / str(owner), _incident(owner_pid=owner, loss=dict(observed_monotonic=10 ** 400)))
            monkeypatch.setattr(hml, "manager_loss_incident", functools.partial(
                _ORIGINAL, root=root, uid=os.getuid(), boot_id=BOOT, host=HOST, now=NOW, check_ancestors=False))
            _claim(conn, tid, 86001 + n, owner_board, owner_pid=owner, birth=BIRTH)
            _reaped(monkeypatch, [86001 + n])
            kbd.detect_crashed_workers(conn)
        assert _task(conn, tid).consecutive_failures == 1
        charged = _last_payload(conn, tid, "crashed")
        assert charged["owner_death_repeat"] == DEAD_OWNER and "host_manager_loss" not in charged


@pytest.mark.parametrize("error", [OverflowError, RuntimeError, KeyError])
def test_the_waiver_fails_closed_on_any_reader_error(owner_board, monkeypatch, error):
    from hermes_cli import kanban_owner_death as od

    def boom(receipt):
        raise error("bad receipt")

    monkeypatch.setattr(hml, "manager_loss_incident", boom)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        assert od._host_manager_loss_waiver(conn, tid, {"pid": DEAD_OWNER, "birth": BIRTH}) is None


def test_non_linux_hosts_never_waive(tmp_path, monkeypatch):
    """Off Linux there is no root watchdog: even a receipt naming this boot exactly returns ``None``."""
    boot_file = "/proc/sys/kernel/random/boot_id"
    if not os.path.exists(boot_file):
        pytest.skip("needs a Linux boot_id for the positive control")
    with open(boot_file, encoding="utf-8") as fh:
        boot = fh.read().strip()
    record = _incident(loss=dict(boot_id=boot))
    root = _root(tmp_path, record)
    kw = dict(root=root, uid=os.getuid(), host=HOST, now=NOW, check_ancestors=False)
    receipt = {"pid": DEAD_OWNER, "birth": BIRTH}
    monkeypatch.setattr(hml.sys, "platform", "linux")
    assert hml.manager_loss_incident(receipt, **kw) == record["incident_id"]  # positive control
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(hml.sys, "platform", platform)
        assert hml.manager_loss_incident(receipt, **kw) is None
