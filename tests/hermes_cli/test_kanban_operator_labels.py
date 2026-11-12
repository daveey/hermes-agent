"""Operator call label_publish: a worker asks the hub to ADD proven skill:/capability: labels to a machine. [daveey]

The two-gateway fleet of ``test_kanban_operator_call`` (worker-5 runs the caller card, ``hub`` is the label home)
plus a fake Board machine registry (the admin CLI's ``machines`` read and the atomic ``machine --add-labels``
delta), a fake hub probe, and a fake Board row op that lands the hub's answer on the requester's card in the
replica, stamped as written by the hub.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as kox
from hermes_cli import kanban_operator_labels as kol
from hermes_cli import kanban_supervisor_requests as ksr
from tests.hermes_cli import test_kanban_operator_call as toc

fleet = toc.fleet
_submit, _round, _log = toc._submit, toc._round, toc._log
_REAL_PROBE = kol._probe


class Registry:
    """The Board machine registry as the admin CLI sees it: ``--add-labels`` adds to the STORED set atomically (the
    Durable Object reads, adds and writes in one transaction) and answers with the resulting row."""

    def __init__(self):
        self.labels = {"worker-3": {"linux", "profile:default", "skill:git", "capability:token-broker"}}
        self.calls: list[tuple] = []
        self.before_write = None  # (name) -> None: another writer whose write lands just before ours
        self.after_write = None
        self.answer = None  # (name, row) -> row: override the Board's answer

    def write(self, name, labels):
        self.labels[name] = set(labels)

    def _row(self, n):
        return {"name": n, "state": "active", "labels": sorted(self.labels[n] | {f"machine:{n}"})}

    def admin(self, *args):
        self.calls.append(args)
        if args[0] == "machines":
            return {"gateways": [self._row(n) for n in self.labels]}
        assert args[0] == "machine" and args[2] == "--add-labels" and len(args) == 4, args
        name, add = args[1], [x for x in args[3].split(",") if x]
        if name not in self.labels:
            raise RuntimeError("hermes_mail.py exited 1: no such gateway")
        if self.before_write:
            self.before_write(name)
        self.labels[name] = self.labels[name] | set(add)  # the delta applies to the stored set, not our read
        row = self._row(name)
        if self.after_write:
            self.after_write(name)
        return self.answer(name, row) if self.answer else row

    def writes(self):
        return [c for c in self.calls if c[0] == "machine"]


@pytest.fixture
def hub(fleet, monkeypatch):
    f = fleet
    reg = Registry()
    probes: list[str] = []
    f.probe = {"complete": True, "skills": ["skill:git", "skill:pdf"],
               "proofs": {"capability:hub-read": {"ok": True, "detail": "forced-command help"},
                          "capability:ssh-mini": {"ok": False, "detail": "Permission denied (publickey)"}}}
    f.push_down = False

    def probe(machine):
        probes.append(machine)
        if isinstance(f.probe, Exception):
            raise f.probe
        return {"machine": machine, **f.probe}

    def push(conn, board, own, task_id, event_uid, payload):
        if f.push_down:
            raise OSError("board unreachable")
        with kb.write_txn(f.rep):  # the Board stores the row; the replica gets it from the feed
            f.rep.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) VALUES (?,?,?,?,?)",
                          (task_id, kox.RESULT_KIND, json.dumps(payload), int(time.time()), event_uid))
        f.origin[event_uid] = own
        return "applied", ""

    monkeypatch.setattr(kol, "_admin", reg.admin)
    monkeypatch.setattr(kol, "_probe", probe)
    monkeypatch.setattr(kol, "_push_event", push)
    monkeypatch.setattr(kol, "_admin_key_present", lambda: True)
    monkeypatch.setattr(kol, "_start", lambda fn: fn())  # the job thread, run inline
    kol._THREAD.clear()
    f.reg, f.probes = reg, probes
    yield f


def _op(**kw):
    return {"op": "operator", "action": "label_publish", "machine": "worker-3", "labels": ["capability:hub-read"],
            "proof": {"kind": "hub_probe"}, **kw}


def _result(f, uid=None):
    rows = f.rep.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                         (f.caller, kox.RESULT_KIND)).fetchall()
    out = [json.loads(r[0]) for r in rows]
    return [p for p in out if uid is None or p["request_uid"] == uid]


def test_accepted_publish_adds_the_delta_and_answers_on_the_requesters_card(hub):
    f = hub
    out = _submit(f, _op(labels=["capability:hub-read", "skill:pdf"]))
    assert out["ops"][0]["verdict"] == "supervisor" and out["ops"][0]["home"] == "hub", out
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done", (req["error"], req["result"])
    assert req["result"][0]["outcome"] == "applied" and req["result"][0]["home"] == "hub"
    # A delta: every label already there is kept, the two proven ones are added, machine:* is never written.
    assert f.reg.labels["worker-3"] == {"linux", "profile:default", "skill:git", "capability:token-broker",
                                        "capability:hub-read", "skill:pdf"}
    assert all("machine:" not in w[3] for w in f.reg.writes())
    assert f.probes == ["worker-3"]  # the hub re-ran its own probe
    (res,) = _result(f)
    assert res["executor_gateway"] == "hub" and res["outcome"] == "applied" and res["machine"] == "worker-3"
    assert "skill:pdf" in res["labels_after"]
    for side, conn in (("request", f.w), ("execute", f.h)):
        (row,) = _log(conn, side)
        assert (row["action"], row["target_machine"], row["caller_task_id"], row["outcome"]) == (
            "label_publish", "worker-3", f.caller, "applied" if side == "execute" else "done"), dict(row)
        assert row["finished_at"]


def test_labels_already_there_are_already_applied_without_a_write(hub):
    f = hub
    out = _submit(f, _op(labels=["capability:hub-read"]))
    f.reg.labels["worker-3"].add("capability:hub-read")
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done" and req["result"][0]["outcome"] == "already_applied"
    assert f.reg.writes() == []


@pytest.mark.parametrize("labels,why", [
    (["machine:worker-3"], "machine:* labels are never published"),
    (["capability:hub-read", "machine:hub"], "machine:* labels are never published"),
    (["profile:default"], "only skill:* and capability:*"),
    (["linux"], "only skill:* and capability:*"),
    (["capability:Hub Read"], "not a valid label"),
])
def test_machine_and_other_namespaces_are_refused(hub, labels, why):
    f = hub
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(f, _op(labels=labels))
    assert exc.value.code == "label_refused" and why in str(exc.value)
    row = _log(f.w, "request")[0]
    assert row["outcome"] == "refused" and row["target_machine"] == "worker-3"
    _round(f)
    assert f.reg.calls == [] and f.probes == []


def test_home_revalidates_a_machine_label_smuggled_past_the_requester(hub):
    """The hub never trusts the request's args: a machine:* label written straight into an event is refused."""
    f = hub
    out = _submit(f, _op())
    ksr.drain(f.w)
    with kb.write_txn(f.w):
        row = f.w.execute("SELECT id, payload FROM task_events WHERE kind = ?", (kox.REQUEST_KIND,)).fetchone()
        p = json.loads(row[1])
        p["args"]["labels"] = ["machine:hub"]
        f.w.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(p), row[0]))
    f.board()
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "machine:* labels are never published" in req["error"]
    assert f.reg.calls == [] and f.probes == []


@pytest.mark.parametrize("proof", [None, {}, {"kind": "trust_me"}, {"kind": "hub_probe", "ok": True}, "hub_probe"])
def test_malformed_proof_is_refused_at_submit(hub, proof):
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(hub, _op(proof=proof))
    assert exc.value.code == "bad_proof"


@pytest.mark.parametrize("labels,why", [
    (["capability:ssh-mini"], "probe failed: Permission denied"),
    (["capability:core-ship"], "no probe proves it"),
    (["skill:xlsx"], "not in worker-3's skill inventory"),
    (["capability:hub-read", "skill:xlsx"], "skill:xlsx"),  # one unproven label refuses the whole call
])
def test_label_the_hub_probe_does_not_prove_is_refused_bad_proof(hub, labels, why):
    f = hub
    before = set(f.reg.labels["worker-3"])
    out = _submit(f, _op(labels=labels))
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "bad_proof" in req["error"] and why in req["error"], req["error"]
    assert f.reg.writes() == [] and f.reg.labels["worker-3"] == before
    assert _log(f.h, "execute")[0]["outcome"] == "refused"


def test_incomplete_inventory_or_another_machines_probe_proves_nothing(hub):
    f = hub
    f.probe = {**f.probe, "complete": False}
    out = _submit(f, _op(labels=["skill:pdf"]))
    _round(f)
    assert "bad_proof" in ksr.get_request(f.w, out["request_id"])["error"]
    f.probe = {**f.probe, "complete": True, "machine": "worker-9"}  # answers for the wrong machine
    out = _submit(f, _op(labels=["capability:hub-read"]))
    _round(f)
    assert "did not answer for worker-3" in ksr.get_request(f.w, out["request_id"])["error"]
    assert f.reg.writes() == []


def test_unreadable_probe_fails_without_publishing(hub):
    f = hub
    f.probe = TimeoutError("SSM inventory timed out")
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "proof_unavailable" in req["error"]
    assert f.reg.writes() == []


def test_hub_without_the_probe_installed_fails_closed(hub, monkeypatch, tmp_path):
    f = hub
    monkeypatch.setattr(kol, "_probe", _REAL_PROBE)
    monkeypatch.setattr(kol, "PROBE_COMMAND", str(tmp_path / "no-such-probe"))
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "proof_unavailable: FileNotFoundError" in req["error"]
    assert f.reg.writes() == []


def test_concurrent_writer_label_survives_the_delta(hub):
    """The CAS-conflict case: another writer adds a label between the hub's read and its write. The Board adds the
    delta to the stored set, so that label survives, in ONE write and with no client-side compare-and-set."""
    f = hub
    f.reg.before_write = lambda name: f.reg.write(name, f.reg.labels[name] | {"capability:tailnet-exec"})
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done" and req["result"][0]["outcome"] == "applied", req
    assert {"capability:hub-read", "capability:tailnet-exec", "linux"} <= f.reg.labels["worker-3"]
    assert f.reg.writes() == [("machine", "worker-3", "--add-labels", "capability:hub-read")]
    (res,) = _result(f)
    assert "capability:tailnet-exec" in res["labels_after"] and "machine:worker-3" not in res["labels_after"]


def test_concurrent_removal_is_kept_not_undone(hub):
    """A writer that removes an unrelated label before our write keeps its removal: the delta never re-adds what it
    read (a whole-set replace would have)."""
    f = hub
    f.reg.before_write = lambda name: f.reg.write(name, f.reg.labels[name] - {"skill:git"})
    out = _submit(f, _op())
    _round(f)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    assert "capability:hub-read" in f.reg.labels["worker-3"] and "skill:git" not in f.reg.labels["worker-3"]


def test_delta_is_sent_as_labels_add_only(hub):
    f = hub
    out = _submit(f, _op(labels=["skill:pdf", "capability:hub-read"]))
    _round(f)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    assert f.reg.writes() == [("machine", "worker-3", "--add-labels", "capability:hub-read,skill:pdf")]
    assert not any("--labels" in c for c in f.reg.calls)


def test_board_answer_without_the_delta_is_a_failure_not_applied(hub):
    """A Board that predates labels_add (or a later writer racing the answer) returns a set without the delta: the
    call fails and says so, it never reports applied."""
    f = hub
    f.reg.answer = lambda name, row: {**row, "labels": [x for x in row["labels"] if x != "capability:hub-read"]}
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "lacks ['capability:hub-read']" in req["error"], req
    (res,) = _result(f)
    assert res["outcome"] == "failed" and res["labels_after"] is None


def test_board_answer_without_a_label_set_is_a_failure(hub):
    f = hub
    f.reg.answer = lambda name, row: {"error": "boom"}
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "carries no label set" in req["error"], req


def test_delta_past_the_board_label_cap_is_refused_without_a_write(hub):
    f = hub
    f.reg.labels["worker-3"] |= {f"skill:s{i}" for i in range(kol.BOARD_MAX_LABELS - 4)}
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "too_many_labels" in req["error"], req
    assert f.reg.writes() == []


def test_delivery_backs_off_after_the_free_retries():
    now = 10_000
    assert all(kol._delivery_due(t, now - 1, now) for t in range(kol.DELIVERY_FREE_RETRIES))
    assert not kol._delivery_due(kol.DELIVERY_FREE_RETRIES, now - 59, now)
    assert kol._delivery_due(kol.DELIVERY_FREE_RETRIES, now - 60, now)
    assert not kol._delivery_due(kol.DELIVERY_FREE_RETRIES + 2, now - 239, now)
    assert not kol._delivery_due(kol.MAX_DELIVERY_ATTEMPTS - 1, now - kol.DELIVERY_BACKOFF_CAP_S + 1, now)
    assert kol._delivery_due(kol.MAX_DELIVERY_ATTEMPTS - 1, now - kol.DELIVERY_BACKOFF_CAP_S, now)
    span = sum(min(60 * 2 ** (t - kol.DELIVERY_FREE_RETRIES), kol.DELIVERY_BACKOFF_CAP_S)
               for t in range(kol.DELIVERY_FREE_RETRIES, kol.MAX_DELIVERY_ATTEMPTS))
    assert 6 * 3600 < span < 24 * 3600  # retries outlast a Board outage but end before the requester's deadline


def test_job_stale_bound_exceeds_the_worst_job():
    worst = kol.PROBE_TIMEOUT_S + kol.ADMIN_CALLS_PER_JOB * kol.ADMIN_TIMEOUT_S
    assert kol.JOB_STALE_S > 2 * worst
    assert not hasattr(kol, "CAS_ROUNDS")  # no client-side compare-and-set loop is left


def test_unknown_machine_is_refused(hub):
    f = hub
    out = _submit(f, _op(machine="worker-77"))
    f.probe = {**f.probe}
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "unknown_machine" in req["error"]


def test_forged_result_on_the_requesters_own_card_is_ignored(hub):
    """The worker owns its card, so it could write a result there itself: only the Board's word that the hub wrote
    it counts."""
    f = hub
    out = _submit(f, _op())
    ksr.drain(f.w)
    uid = json.loads(f.w.execute("SELECT payload FROM task_events WHERE kind = ?",
                                 (kox.REQUEST_KIND,)).fetchone()[0])["request_uid"]
    with kb.write_txn(f.w):
        kb._append_event(f.w, f.caller, kox.RESULT_KIND, {"request_uid": uid, "action": "label_publish",
                                                          "outcome": "applied", "executor_gateway": "hub"})
    f.board()  # the Board says worker-5 wrote it
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "accepted"
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    assert "capability:hub-read" in f.reg.labels["worker-3"]


def test_unreachable_board_keeps_the_answer_until_it_is_delivered(hub):
    f = hub
    out = _submit(f, _op())
    ksr.drain(f.w)
    f.board()
    f.push_down = True
    ksr.drain(f.h)
    job = f.h.execute("SELECT state, delivered_at, delivery_attempts FROM operator_label_jobs").fetchone()
    assert (job[0], job[1], job[2]) == ("done", None, 1)
    f.push_down = False
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    assert len(_result(f)) == 1 and len(f.reg.writes()) == 1  # published once, answered once


def test_replayed_request_is_not_published_twice(hub):
    f = hub
    _submit(f, _op())
    _round(f)
    kox._MARK.clear()
    with kb.write_txn(f.h):
        f.h.execute("DELETE FROM operator_call_scan")
    kox.apply_incoming(f.h)
    assert len(f.reg.writes()) == 1 and len(f.probes) == 1


def test_a_job_that_keeps_dying_fails_after_bounded_attempts(hub, monkeypatch):
    f = hub
    out = _submit(f, _op())
    ksr.drain(f.w)
    f.board()
    monkeypatch.setattr(kol, "_start", lambda fn: None)  # the thread never runs (a crash every time)
    kox.apply_incoming(f.h)
    now = int(time.time())
    for i in range(kol.MAX_JOB_ATTEMPTS):
        kol.tick(f.h, "hub", "default", now=now + (i + 1) * (kol.JOB_STALE_S + 1))
    row = f.h.execute("SELECT state, outcome, attempts FROM operator_label_jobs").fetchone()
    assert (row[0], row[1], row[2]) == ("done", "failed", kol.MAX_JOB_ATTEMPTS)
    f.board()
    ksr.drain(f.w)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "stopped" in req["error"]


def test_hub_without_the_admin_key_refuses_and_never_runs_an_admin_command(hub, monkeypatch, tmp_path):
    f = hub
    monkeypatch.setattr(kol, "ADMIN_KEY", str(tmp_path / "absent" / "admin.key"))
    monkeypatch.setattr(kol, "_admin_key_present", _real_key_check)
    out = _submit(f, _op())
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "no_admin_key" in req["error"]
    assert f.reg.calls == [] and f.probes == []


def _real_key_check():
    from pathlib import Path
    return Path(kol.ADMIN_KEY).expanduser().is_file()


def test_workers_never_touch_an_admin_key_or_execute_label_publish(hub, tmp_path, monkeypatch):
    """Behaviour: with an admin key on disk, the worker's whole side (submit, forward, poll) never opens it or
    spawns an admin command, and a non-hub gateway's home tick never takes a label_publish call, even one that
    names it as home."""
    import builtins
    import io
    import os
    import subprocess
    f = hub
    key = tmp_path / ".config" / "hermes-mail" / "admin.key"
    key.parent.mkdir(parents=True)
    key.write_text("secret")
    monkeypatch.setattr(kol, "ADMIN_KEY", str(key))
    out = _submit(f, _op())
    ksr.drain(f.w)
    with kb.write_txn(f.w):  # a request that names worker-5 itself as the label home
        row = f.w.execute("SELECT payload FROM task_events WHERE kind = ?", (kox.REQUEST_KIND,)).fetchone()
        kb._append_event(f.w, f.caller, kox.REQUEST_KIND, {**json.loads(row[0]), "request_uid": "self-home",
                                                           "home": "worker-5"})
    f.board()
    opened, spawned = [], []
    real_open, real_os_open, real_popen = builtins.open, os.open, subprocess.Popen

    def spy_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        opened.append(str(path))
        return real_os_open(path, *a, **k)

    class SpyPopen(real_popen):
        def __init__(self, args, *a, **k):
            spawned.append(args)
            super().__init__(args, *a, **k)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(kol, "_admin_key_present", _real_key_check)
    calls_before = len(f.reg.calls)
    kox.apply_incoming(f.w)  # worker-5's home tick
    ksr.drain(f.w)
    assert not [p for p in opened if "admin" in p], opened
    assert not [a for a in spawned if "admin" in json.dumps(a, default=str)], spawned
    assert len(f.reg.calls) == calls_before and f.probes == []
    assert not f.w.execute("SELECT 1 FROM sqlite_master WHERE name = 'operator_label_jobs'").fetchone() or \
        not f.w.execute("SELECT 1 FROM operator_label_jobs").fetchone()
    assert ksr.get_request(f.w, out["request_id"])["status"] == "accepted"  # still waiting on the hub


def test_dry_run_reports_the_hub_as_executor(hub):
    out = _submit(hub, _op(), dry_run=True)
    assert out["ops"][0]["verdict"] == "supervisor" and out["ops"][0]["home"] == kol.HOME
    assert _log(hub.w) == []


def test_label_publish_takes_no_card(hub):
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(hub, _op(task_id=hub.caller))
    assert exc.value.code == "invalid_ops" and "names a machine" in str(exc.value)


def test_result_payload_is_bound_to_this_request(hub):
    """Two calls in flight: each request finishes on its own answer."""
    f = hub
    a = _submit(f, _op(labels=["capability:hub-read"]))
    b = _submit(f, _op(labels=["skill:xlsx"]))
    _round(f)
    _round(f)  # one label job per hub tick: the second call runs on the next one
    assert ksr.get_request(f.w, a["request_id"])["status"] == "done"
    assert ksr.get_request(f.w, b["request_id"])["status"] == "rejected"
    assert {p["outcome"] for p in _result(f)} == {"applied", "refused"}
    assert len({p["request_uid"] for p in _result(f)}) == 2
