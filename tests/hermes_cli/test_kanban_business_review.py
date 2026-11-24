"""CONTRACT199 rev6: host zero-tool business review adapter.

Fake Hub, temporary kanban home, real SQLite board; no provider network and no
production board. Numbers in test names follow the contract's test list.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_business_review as br
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_sync_client import HubError, Resync

REPO = str(Path(__file__).resolve().parents[2])
FORBIDDEN_LEASE_ACTIONS = {"renew", "release", "retain", "contain"}


# ------------------------------------------------------------------ fixtures

class FakeHub:
    """Scripted Board. Receipt items: symbolic class names, raw dicts, or exceptions."""

    def __init__(self):
        self.calls: list = []
        self.probe_reply = {"results": [], "head": 7}
        self.receipts: list = []
        self.receipt_default = "absent"
        self.lease_replies: list = []
        self.ops_replies: list = []
        self.leases_sent: list = []
        self.ops_sent: list = []
        self.deadline_ms = None

    # -- transport
    def _call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            reply = self.probe_reply
            if isinstance(reply, BaseException):
                raise reply
            return reply
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
        item = self.receipts.pop(0) if self.receipts else self.receipt_default
        if callable(item) and not isinstance(item, type):
            item = item(query)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return receipt_body(item, query)
        return item

    def leases(self, board, body):
        self.calls.append(("LEASES", board, copy.deepcopy(body)))
        self.leases_sent.append(copy.deepcopy(body))
        reply = self.lease_replies.pop(0) if self.lease_replies else "admit"
        if callable(reply) and not isinstance(reply, str):
            reply = reply(body)
        if isinstance(reply, BaseException):
            raise reply
        if reply == "admit":
            return admit(body, self.deadline_ms)
        return reply

    def ops(self, board, ops):
        self.calls.append(("OPS", board, copy.deepcopy(ops)))
        self.ops_sent.append(copy.deepcopy(ops))
        reply = self.ops_replies.pop(0) if self.ops_replies else {"results": [{"status": "applied"}], "head": 8}
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def hub_calls(self):
        return [c for c in self.calls]


def admit(body, deadline_ms=None):
    ra = body["review_admit"]
    return {"status": "granted", "review_admission": {
        "task": body["task"], "run_uid": body["run_uid"], "admission_uid": ra["admission_uid"],
        "operation_uid": ra["operation_uid"],
        "envelope_digest": hashlib.sha256(br.canonical(ra)).hexdigest(),
        "hard_deadline_ms": deadline_ms if deadline_ms is not None else int(time.time() * 1000) + 900_000}}


NO_EFFECT = {"restart_permitted": False, "settlement_permitted": False, "resend_permitted": False}


def receipt_body(kind, query):
    rec = {"task": query.get("task"), "run_uid": query.get("run"), "operation_uid": query.get("operation")}
    if kind == "unavailable":
        raise HubError(503, "reviewReceipt not yet available")
    table = {
        "absent": ("absent", None),
        "live": ("admitted", None),
        "approved": ("applied", {**rec, "verdict": "approved"}),
        "repair": ("applied", {**rec, "verdict": "changes_requested"}),
        "closed": ("rejected", {**rec, "verdict": "malformed"}),
        "sealed": ("sealed", None),
    }
    state, receipt = table[kind]
    return {"state": state, "receipt": receipt, **NO_EFFECT}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in br.CHILD_ENV_DROP:
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    conn = kbc.connect()
    hub = FakeHub()
    yield SimpleNamespace(home=home, conn=conn, hub=hub, board="default")
    conn.close()
    # Contract: no renew/release/retain/contain and no op other than the one settlement, ever.
    for body in hub.leases_sent:
        assert body.get("action") == "grant", body
        assert body.get("action") not in FORBIDDEN_LEASE_ACTIONS
    per_task: dict = {}
    for ops in hub.ops_sent:
        assert len(ops) == 1 and ops[0]["op"] in ("complete", "request_changes")
        per_task.setdefault(ops[0]["task"], set()).add(json.dumps(ops, sort_keys=True))
    assert all(len(distinct) == 1 for distinct in per_task.values()), hub.ops_sent


DIFF = b"diff --git a/x b/x\n+hello \xc3\xa9\n"


def make_review(env, *, meta=None, drop=(), diff=DIFF, links=True):
    conn = env.conn
    repair = kb.create_task(conn, title="repair target", assignee="default")
    review = kb.create_task(conn, title="business review", assignee="default")
    landing = kb.create_task(conn, title="landing", assignee="default")
    diffs = env.home / "diffs"
    diffs.mkdir(exist_ok=True)
    diff_path = diffs / f"{review}.diff"
    diff_path.write_bytes(diff)
    metadata = {
        "author_session": "author-sess", "repo": "example/repo", "head_sha": "a" * 40, "base_sha": "b" * 40,
        "full_diff_sha256": hashlib.sha256(diff).hexdigest(), "policy_version": "bb-zero-tool-v1",
        "prompt_digest": br.INSTRUCTION_DIGEST, "repair_target": repair, "landing_task": landing,
        "diff_path": str(diff_path),
    }
    metadata.update(meta or {})
    for key in drop:
        metadata.pop(key, None)
    assert kb.claim_task(conn, review) is not None
    assert kb.request_review(conn, review, summary="ready for business review", metadata=metadata)
    if links:
        kb.link_tasks(conn, metadata.get("repair_target", repair), review)
        kb.link_tasks(conn, review, metadata.get("landing_task", landing))
    assert kb.get_task(conn, review).status == "review"
    return SimpleNamespace(review=review, repair=repair, landing=landing, meta=metadata, diff=diff,
                           diff_path=diff_path)


def set_run_metadata(env, task, raw):
    run_id = env.conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='review_requested' "
                              "ORDER BY id DESC LIMIT 1", (task,)).fetchone()[0]
    with env.conn:
        env.conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (raw, run_id))


def sidecar_rows(env):
    path = env.home / "business-review" / f"{env.board}.db"
    if not path.exists():
        return []
    side = sqlite3.connect(str(path))
    side.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in side.execute("SELECT * FROM operations")]
    finally:
        side.close()


def row_of(env, task):
    rows = [r for r in sidecar_rows(env) if r["task_id"] == task]
    assert len(rows) <= 1
    return rows[0] if rows else None


def set_row(env, task, **fields):
    path = env.home / "business-review" / f"{env.board}.db"
    side = sqlite3.connect(str(path))
    with side:
        side.execute(f"UPDATE operations SET {', '.join(f'{k}=?' for k in fields)} WHERE task_id=?",
                     (*fields.values(), task))
    side.close()


def prepare(env, rv):
    return br.prepare(env.conn, env.hub, env.board, rv.review)


VERDICT_APPROVE = json.dumps({"verdict": "approve", "summary": "Looks right \u00e9", "findings": []})
VERDICT_CHANGES = json.dumps({"verdict": "request_changes", "summary": "Fix it",
                              "findings": [{"title": "Bug", "body": "Off by one"}]})


def fake_child_argv(text=VERDICT_APPROVE, sleep=0.0, exit_code=0):
    """Real subprocess child that writes ``text`` as result.json through the module's child entry."""
    def argv(work_dir, wall_seconds, prompt_sha256):
        script = (
            f"import sys, time; sys.path.insert(0, {REPO!r})\n"
            "import hermes_cli.kanban_business_review as m\n"
            "def fake(messages):\n"
            f"    time.sleep({sleep!r})\n"
            f"    return {text!r}\n"
            "m.complete_fn = fake\n"
            "m._tool_inventory = lambda: []\n"
            "sys.exit(m._child_entry(sys.argv[1], int(sys.argv[2]), sys.argv[3]))\n"
        )
        return [sys.executable, "-c", script, work_dir, str(wall_seconds), prompt_sha256]
    return argv


@pytest.fixture
def spawns(monkeypatch):
    seen = []
    real = subprocess.Popen

    def counting(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(br.subprocess, "Popen", counting)
    monkeypatch.setattr(br, "_child_argv", fake_child_argv())
    return seen


def to_admitted(env, rv):
    """Grant admitted, then the post-admission receipt read is unavailable: stop at ``admitted``."""
    env.hub.receipts = ["unavailable"]
    assert prepare(env, rv) == "unavailable"
    assert row_of(env, rv.review)["local_state"] == "admitted"


# --------------------------------------------------------------- 1. vectors

def test_01_envelope_vector_and_settlement_hash():
    envelope = {"author_session": "author-sess", "base_sha": "b" * 40,
                "capability_digest": br.EMPTY_CAPABILITY_DIGEST, "full_diff_sha256": "c" * 64,
                "head_sha": "a" * 40, "landing_task": "t_land", "policy_version": "bb-zero-tool-v1",
                "prompt_digest": "d" * 64, "repair_target": "t_repair", "repo": "example/repo",
                "review_task": "t_review", "reviewer_session": "review-sess"}
    assert br.EMPTY_CAPABILITY_DIGEST == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert br.envelope_digest(envelope, "adm-0001", "op-00001") == \
        "0a2fe0caf1e291d7e5e1e42f46c8b1b5ca01eb109d536281392c33ec4daed650"
    assert br.INSTRUCTION_DIGEST == "fb6b4244630f4a7148cf4d7a93a6a486d1cf0b22e653cc068e764bb2369cddcc"
    row = {"task_id": "t_review", "run_uid": "run-1", "operation_uid": "op-1",
           "capability_digest": br.EMPTY_CAPABILITY_DIGEST, "settle_op_id": "cop-1"}
    op = br.settlement_op(row, {"verdict": "approve", "summary": "Gr\u00fc\u00dfe \u2713", "findings": []})
    literal = ('{"capability_digest":"' + br.EMPTY_CAPABILITY_DIGEST + '","op":"complete","operation_uid":"op-1","result":"Gr\u00fc\u00dfe \u2713",'
               '"run_uid":"run-1","summary":"Gr\u00fc\u00dfe \u2713","task":"t_review"}')
    assert op["payload_hash"] == hashlib.sha256(literal.encode("utf-8")).hexdigest()
    assert op["client_op_id"] == "cop-1"


# --------------------------------------------------------- 2-3. qualification

UNQUALIFIED_META = [
    ("missing_" + key, {}, (key,)) for key in br.METADATA_KEYS
] + [
    ("non_string", {"repo": 7}, ()),
    ("too_long", {"repo": "r" * 257}, ()),
    ("bad_head", {"head_sha": "A" * 40}, ()),
    ("bad_base", {"base_sha": "b" * 39}, ()),
    ("bad_diff_hex", {"full_diff_sha256": "g" * 64}, ()),
    ("bad_prompt_hex", {"prompt_digest": "x" * 64}, ()),
    ("wrong_policy", {"policy_version": "bb-zero-tool-v2"}, ()),
    ("wrong_prompt_digest", {"prompt_digest": "e" * 64}, ()),
    ("diff_mismatch", {"full_diff_sha256": "f" * 64}, ()),
]


@pytest.mark.parametrize("name,meta,drop", UNQUALIFIED_META, ids=[c[0] for c in UNQUALIFIED_META])
def test_02_unqualified_metadata(env, name, meta, drop):
    rv = make_review(env, meta=meta, drop=drop)
    assert prepare(env, rv) == "unqualified"
    assert env.hub.calls == [] and sidecar_rows(env) == []


def test_02_unqualified_board_shapes(env):
    assert br.prepare(env.conn, env.hub, env.board, "t_missing") == "unqualified"
    plain = kb.create_task(env.conn, title="no event", assignee="default")
    assert br.prepare(env.conn, env.hub, env.board, plain) == "unqualified"
    # NULL run_id: never-claimed review without summary/metadata.
    nul = kb.create_task(env.conn, title="null run", assignee="default")
    assert kb.request_review(env.conn, nul)
    assert br.prepare(env.conn, env.hub, env.board, nul) == "unqualified"
    rv = make_review(env)
    set_run_metadata(env, rv.review, json.dumps(["not", "object"]))
    assert prepare(env, rv) == "unqualified"
    rv2 = make_review(env)
    set_run_metadata(env, rv2.review, "not json")
    assert prepare(env, rv2) == "unqualified"
    rv3 = make_review(env)
    with env.conn:
        env.conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='review_requested'",
                         (json.dumps({"implementer": "  "}), rv3.review))
    assert prepare(env, rv3) == "unqualified"
    rv4 = make_review(env)
    with env.conn:
        run_id = env.conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='review_requested'",
                                  (rv4.review,)).fetchone()[0]
        env.conn.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='review_requested'",
                         (run_id + 10_000, rv4.review))
    assert prepare(env, rv4) == "unqualified"
    assert env.hub.calls == [] and sidecar_rows(env) == []


def test_02_unqualified_status_links_and_ids(env):
    running = make_review(env)
    with env.conn:
        env.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running.review,))
    assert prepare(env, running) == "unqualified"
    missing = make_review(env, links=False)
    kb.link_tasks(env.conn, missing.repair, missing.review)
    assert prepare(env, missing) == "unqualified"
    reversed_ = make_review(env, links=False)
    kb.link_tasks(env.conn, reversed_.review, reversed_.repair)
    kb.link_tasks(env.conn, reversed_.review, reversed_.landing)
    assert prepare(env, reversed_) == "unqualified"
    equal = make_review(env, links=False)
    set_meta = dict(equal.meta, landing_task=equal.repair)
    set_run_metadata(env, equal.review, json.dumps(set_meta))
    kb.link_tasks(env.conn, equal.repair, equal.review)
    assert prepare(env, equal) == "unqualified"
    assert env.hub.calls == [] and sidecar_rows(env) == []


def test_02_unqualified_diff_paths(env, tmp_path):
    outside = tmp_path / "outside.diff"
    outside.write_bytes(DIFF)
    rv = make_review(env)
    set_run_metadata(env, rv.review, json.dumps(dict(rv.meta, diff_path=str(outside))))
    assert prepare(env, rv) == "unqualified"
    rv2 = make_review(env)
    link = env.home / "diffs" / "link.diff"
    link.symlink_to(rv2.diff_path)
    set_run_metadata(env, rv2.review, json.dumps(dict(rv2.meta, diff_path=str(link))))
    assert prepare(env, rv2) == "unqualified"
    big = b"x" * (br.MAX_DIFF + 1)
    rv3 = make_review(env, diff=big)
    assert prepare(env, rv3) == "unqualified"
    rv4 = make_review(env)
    folder = env.home / "diffs" / "dir.diff"
    folder.mkdir()
    set_run_metadata(env, rv4.review, json.dumps(dict(rv4.meta, diff_path=str(folder))))
    assert prepare(env, rv4) == "unqualified"
    assert env.hub.calls == [] and sidecar_rows(env) == []


def test_02_diff_of_exactly_one_mib_qualifies(env):
    rv = make_review(env, diff=b"y" * br.MAX_DIFF)
    env.hub.lease_replies = [{"status": "other"}]
    assert prepare(env, rv) == "unavailable"
    assert row_of(env, rv.review)["local_state"] == "grant_sent"


def test_03_reviewer_session_never_author(env, monkeypatch):
    rv = make_review(env, meta={"author_session": "rev-" + "0" * 32})
    with monkeypatch.context() as patched:
        patched.setattr(br.secrets, "token_hex", lambda n: "0" * 32)
        assert prepare(env, rv) == "unqualified"
    assert env.hub.calls == []
    rv2 = make_review(env)
    env.hub.lease_replies = [{"status": "other"}]
    prepare(env, rv2)
    row = row_of(env, rv2.review)
    assert row["reviewer_session"].startswith("rev-") and row["reviewer_session"] != row["author_session"]


# --------------------------------------------------- 4-6. lock, probe, snapshot

def test_04_task_lock_held(env):
    import fcntl
    rv = make_review(env)
    lock_dir = env.home / "business-review" / env.board
    lock_dir.mkdir(parents=True)
    fd = os.open(str(lock_dir / f"{rv.review}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert prepare(env, rv) == "unavailable"
    finally:
        os.close(fd)
    assert env.hub.calls == [] and not (env.home / "business-review" / f"{env.board}.db").exists()


@pytest.mark.parametrize("reply", [HubError(409, "stream_legacy_fenced"), Resync("floor"), OSError("down"),
                                   TimeoutError("slow"), ["not", "dict"], {"results": [1]}, {"head": 1}],
                         ids=["409", "resync", "oserror", "timeout", "list", "nonempty", "no_results"])
def test_05_probe_failures(env, reply):
    rv = make_review(env)
    env.hub.probe_reply = reply
    assert prepare(env, rv) == "unavailable"
    assert env.hub.leases_sent == [] and sidecar_rows(env) == []
    assert [c[0] for c in env.hub.calls] == ["POST"]


def test_05_probe_shape(env):
    rv = make_review(env)
    env.hub.lease_replies = [{"status": "other"}]
    prepare(env, rv)
    assert env.hub.calls[0] == ("POST", f"/v2/boards/{env.board}/ops", {"ops": [], "probe": True})


def test_06_prompt_snapshot_modes(env):
    rv = make_review(env)
    env.hub.lease_replies = [{"status": "other"}]
    assert prepare(env, rv) == "unavailable"
    row = row_of(env, rv.review)
    work = Path(row["work_dir"])
    prompt = (work / "prompt.txt").read_bytes()
    assert prompt == br.INSTRUCTION + b"\n" + DIFF
    assert stat.S_IMODE((work / "prompt.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    assert stat.S_IMODE((env.home / "business-review" / f"{env.board}.db").stat().st_mode) == 0o600
    assert row["prompt_sha256"] == hashlib.sha256(prompt).hexdigest()


def test_06_existing_prompt_is_unavailable(env, monkeypatch):
    rv = make_review(env)
    fixed = iter(["1" * 32, "2" * 32, "3" * 32, "4" * 32, "5" * 32, "6" * 32])
    monkeypatch.setattr(br.secrets, "token_hex", lambda n: next(fixed))
    work = env.home / "business-review" / env.board / ("op-" + "4" * 32)
    work.mkdir(parents=True)
    (work / "prompt.txt").write_bytes(b"other")
    assert prepare(env, rv) == "unavailable"
    assert env.hub.leases_sent == [] and sidecar_rows(env) == []


# ------------------------------------------------------------------ 7-15c. grant

def test_07_grant_shape(env):
    rv = make_review(env)
    env.hub.lease_replies = [{"status": "other"}]
    prepare(env, rv)
    (grant,) = env.hub.leases_sent
    row = row_of(env, rv.review)
    assert set(grant) == {"action", "task", "run_uid", "lane", "ttl_s", "client_op_id", "review_admit"}
    assert grant["action"] == "grant" and grant["lane"] == "review" and grant["ttl_s"] == 900
    assert grant["task"] == rv.review and grant["run_uid"] == row["run_uid"]
    assert grant["client_op_id"] == row["grant_op_id"] and grant["client_op_id"].startswith("cop-")
    ra = grant["review_admit"]
    assert ra["protocol"] == 1 and ra["admission_uid"] == row["admission_uid"]
    assert set(ra) == {"protocol", "admission_uid", "operation_uid", *br.ENVELOPE_KEYS}
    assert ra["capability_digest"] == br.EMPTY_CAPABILITY_DIGEST
    assert hashlib.sha256(br.canonical(ra)).hexdigest() == row["envelope_digest"]


def _refused(env, rv):
    env.hub.lease_replies = [{"status": "rejected", "reason": "custody_open"}]
    env.hub.receipts = ["absent"]
    assert prepare(env, rv) == "refused"


def test_08_rejected_then_absent_is_refused(env):
    rv = make_review(env)
    _refused(env, rv)
    row = row_of(env, rv.review)
    assert row["local_state"] == "grant_refused" and len(env.hub.leases_sent) == 1


def test_09_refused_resend_unchanged_handoff(env):
    rv = make_review(env)
    _refused(env, rv)
    first = env.hub.leases_sent[0]
    env.hub.receipts = ["absent"]
    env.hub.lease_replies = [{"status": "other"}]
    assert prepare(env, rv) == "unavailable"
    second = env.hub.leases_sent[1]
    assert second["client_op_id"] != first["client_op_id"] and second["client_op_id"].startswith("cop-")
    assert {k: v for k, v in second.items() if k != "client_op_id"} == \
        {k: v for k, v in first.items() if k != "client_op_id"}


def test_09_refused_changed_handoff_sends_nothing(env):
    rv = make_review(env)
    _refused(env, rv)
    set_run_metadata(env, rv.review, json.dumps(dict(rv.meta, head_sha="9" * 40)))
    before = len(env.hub.calls)
    env.hub.receipts = ["absent"]
    assert prepare(env, rv) == "refused"
    assert [c[0] for c in env.hub.calls[before:]] == ["GET"]
    assert len(env.hub.leases_sent) == 1 and row_of(env, rv.review)["local_state"] == "grant_refused"


def test_10_rejected_then_live_retires(env, spawns):
    rv = make_review(env)
    env.hub.lease_replies = [{"status": "rejected"}]
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    assert spawns == []


def test_11_no_field_body_and_budget(env):
    rv = make_review(env)
    env.hub.lease_replies = [{"status": "queued"}] * 4
    assert prepare(env, rv) == "unavailable"
    assert row_of(env, rv.review)["local_state"] == "grant_sent"
    for _ in range(3):
        env.hub.receipts = ["absent"]
        assert prepare(env, rv) == "unavailable"
    sent = env.hub.leases_sent
    assert len(sent) == 4 and all(s == sent[0] for s in sent)
    env.hub.receipts = ["absent"]
    assert prepare(env, rv) == "retired_unknown"
    assert len(env.hub.leases_sent) == 4 and row_of(env, rv.review)["grant_attempts"] == 4


@pytest.mark.parametrize("field", ["task", "run_uid", "admission_uid", "operation_uid", "envelope_digest",
                                   "hard_deadline_ms"])
def test_12_admission_mismatch_retires(env, spawns, field):
    rv = make_review(env)

    def reply(body):
        out = admit(body)
        if field == "hard_deadline_ms":
            del out["review_admission"][field]
        else:
            out["review_admission"][field] = "other"
        return out
    env.hub.lease_replies = [reply]
    assert prepare(env, rv) == "retired_unknown"
    assert spawns == []


def test_13_grant_timeout(env):
    rv = make_review(env)
    env.hub.lease_replies = [TimeoutError("slow")]
    assert prepare(env, rv) == "unavailable"
    assert row_of(env, rv.review)["local_state"] == "grant_sent"


def test_14_live_with_grant_sent_no_admission_no_spawn(env, spawns):
    rv = make_review(env)
    env.hub.lease_replies = [OSError("lost"), {"status": "queued"}]
    assert prepare(env, rv) == "unavailable"
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "unavailable"
    assert spawns == [] and row_of(env, rv.review)["local_state"] == "grant_sent"


def test_15_grant_sent_with_approved_receipt(env):
    rv = make_review(env)
    env.hub.lease_replies = [OSError("lost")]
    prepare(env, rv)
    env.hub.receipts = ["approved"]
    sent = len(env.hub.leases_sent)
    assert prepare(env, rv) == "approved"
    row = row_of(env, rv.review)
    assert row["local_state"] == "done" and row["last_class"] == "approved"
    assert len(env.hub.leases_sent) == sent and env.hub.ops_sent == []


def test_15a_refused_row_with_live_resends_original(env, spawns, monkeypatch):
    rv = make_review(env)
    _refused(env, rv)
    first = env.hub.leases_sent[0]
    minted = []
    real = br._new_id
    monkeypatch.setattr(br, "_new_id", lambda p: minted.append(p) or real(p))
    env.hub.receipts = ["live", "unavailable"]
    assert prepare(env, rv) == "unavailable"
    assert env.hub.leases_sent[1] == first and minted == []
    assert row_of(env, rv.review)["local_state"] == "admitted"


def test_15b_identity_stable_across_refusals(env):
    rv = make_review(env)
    _refused(env, rv)
    row0 = row_of(env, rv.review)
    prompt0 = Path(row0["work_dir"], "prompt.txt").read_bytes()
    for _ in range(3):
        env.hub.lease_replies = [{"status": "rejected"}]
        env.hub.receipts = ["absent", "absent"]
        assert prepare(env, rv) == "refused"
    env.hub.receipts = ["absent"]
    assert prepare(env, rv) == "retired_unknown"
    sent = env.hub.leases_sent
    assert len(sent) == 4
    assert len({s["client_op_id"] for s in sent}) == 4
    for s in sent:
        assert {k: v for k, v in s.items() if k != "client_op_id"} == \
            {k: v for k, v in sent[0].items() if k != "client_op_id"}
    row = row_of(env, rv.review)
    assert [row[k] for k in ("run_uid", "admission_uid", "operation_uid", "envelope_digest")] == \
        [row0[k] for k in ("run_uid", "admission_uid", "operation_uid", "envelope_digest")]
    assert row["grant_attempts"] == 4
    assert Path(row["work_dir"], "prompt.txt").read_bytes() == prompt0


def test_15c_delayed_copy_admits_same_custody_one_child(env, spawns):
    rv = make_review(env)
    _refused(env, rv)
    first = env.hub.leases_sent[0]
    # The re-send is refused again, but the delayed first copy has meanwhile been admitted.
    env.hub.lease_replies = [{"status": "rejected"}, lambda body: admit(first)]
    env.hub.receipts = ["absent", "live"]
    assert prepare(env, rv) == "retired_unknown"
    assert spawns == []
    # A different path: the delayed copy is admitted before the re-send, the re-send returns that custody.
    rv2 = make_review(env)
    _refused(env, rv2)
    first2 = env.hub.leases_sent[-1]
    env.hub.lease_replies = [lambda body: admit(first2)]
    env.hub.receipts = ["absent", "live", "approved"]
    assert prepare(env, rv2) == "approved"
    assert len(spawns) == 1
    env.hub.receipts = ["approved"]
    assert prepare(env, rv2) == "approved" and len(spawns) == 1


# ------------------------------------------------------------- 16-18. receipt

IDENT = ("t_r", "run-1", "adm-1", "op-1", "e" * 64)


def _classify(body):
    hub = FakeHub()
    hub.receipts = [body]
    return br.read_receipt(hub, "default", IDENT)


def _rec(**over):
    rec = {"task": "t_r", "run_uid": "run-1", "operation_uid": "op-1"}
    rec.update(over)
    return rec


@pytest.mark.parametrize("body,expected", [
    (HubError(400, "bad"), "refused"), (HubError(401, "auth"), "refused"), (HubError(403, "no"), "refused"),
    (HubError(503, "later"), "unavailable"), (HubError(404, "x"), "unavailable"), (Resync("f"), "unavailable"),
    (OSError("x"), "unavailable"), (TimeoutError("x"), "unavailable"), (ValueError("json"), "unavailable"),
    (["list"], "unavailable"),
    ({"state": "absent", "receipt": None, **NO_EFFECT, "resend_permitted": True}, "unavailable"),
    ({"state": "absent", "receipt": None, "restart_permitted": False, "settlement_permitted": False},
     "unavailable"),
    ({"state": "absent", "receipt": None, **NO_EFFECT, "settlement_permitted": 0}, "unavailable"),
    ({"state": "absent", "receipt": None, **NO_EFFECT}, "absent"),
    ({"state": "admitted", "receipt": None, **NO_EFFECT}, "live"),
    ({"state": "applied", "receipt": _rec(verdict="approved"), **NO_EFFECT}, "approved"),
    ({"state": "applied", "receipt": _rec(verdict="changes_requested"), **NO_EFFECT}, "repair"),
    ({"state": "rejected", "receipt": _rec(verdict="malformed"), **NO_EFFECT}, "closed"),
    ({"state": "sealed", "receipt": None, **NO_EFFECT}, "closed"),
    ({"state": "sealed", "receipt": _rec(verdict="no_verdict"), **NO_EFFECT}, "closed"),
    ({"state": "applied", "receipt": None, **NO_EFFECT}, "unavailable"),
    ({"state": "applied", "receipt": "approved", **NO_EFFECT}, "unavailable"),
    ({"state": "applied", "receipt": _rec(verdict="maybe"), **NO_EFFECT}, "unavailable"),
    ({"state": "applied", "receipt": _rec(verdict="approved", task="t_x"), **NO_EFFECT}, "unavailable"),
    ({"state": "applied", "receipt": _rec(verdict="approved", run_uid="run-x"), **NO_EFFECT}, "unavailable"),
    ({"state": "applied", "receipt": _rec(verdict="approved", operation_uid="op-x"), **NO_EFFECT},
     "unavailable"),
    ({"state": "weird", "receipt": None, **NO_EFFECT}, "unavailable"),
])
def test_16_receipt_classification(body, expected):
    assert _classify(body) == expected


def test_16_receipt_path():
    hub = FakeHub()
    hub.receipts = ["absent"]
    br.read_receipt(hub, "my board", ("t r", "run&1", "adm=1", "op/1", "e" * 64))
    method, path, body = hub.calls[0]
    assert method == "GET" and body is None
    split = urllib.parse.urlsplit(path)
    assert split.path == "/v2/boards/my%20board/business-commands/review-receipt"
    assert urllib.parse.parse_qsl(split.query) == [("task", "t r"), ("run", "run&1"), ("admission", "adm=1"),
                                                   ("operation", "op/1"), ("envelope", "e" * 64)]


def test_17_qualify(env):
    for body, expected in [({"state": "applied", "receipt": _rec(verdict="approved"), **NO_EFFECT}, "approved"),
                           ({"state": "applied", "receipt": _rec(verdict="changes_requested"), **NO_EFFECT},
                            "not_approved"),
                           ({"state": "admitted", "receipt": None, **NO_EFFECT}, "not_approved"),
                           (HubError(503, "x"), "not_approved")]:
        hub = FakeHub()
        hub.receipts = [body]
        assert br.qualify(hub, env.board, IDENT) == expected
    assert not (env.home / "business-review").exists()


@pytest.mark.parametrize("state", ["admitted", "executing", "result_held", "settle_sent", "retired_unknown"])
def test_18_preflight_rule(env, spawns, state):
    rv = make_review(env)
    to_admitted(env, rv)
    set_row(env, rv.review, local_state=state,
            settle_json=json.dumps({"op": "complete", "task": rv.review}))
    before = (len(env.hub.leases_sent), len(env.hub.ops_sent))
    for cls in ("unavailable", "absent"):
        env.hub.receipts = [cls]
        expected = "retired_unknown" if (state == "retired_unknown" and cls == "absent") else "unavailable"
        assert prepare(env, rv) == expected
        assert row_of(env, rv.review)["local_state"] == state
    env.hub.receipts = ["approved"]
    assert prepare(env, rv) == "approved"
    assert row_of(env, rv.review)["local_state"] == "done"
    assert (len(env.hub.leases_sent), len(env.hub.ops_sent)) == before and spawns == []


# ------------------------------------------------------------ 19-30. execution

def test_19_child_env_and_argv(monkeypatch):
    for key in br.CHILD_ENV_DROP:
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = br._child_env()
    assert not any(k in env for k in br.CHILD_ENV_DROP) and env["KEEP_ME"] == "1"
    assert br._child_argv("/w", 30, "f" * 64) == [sys.executable, "-m", "hermes_cli.kanban_business_review",
                                                  "--child", "/w", "30", "f" * 64]


@pytest.mark.parametrize("margin,spawned", [(120000, False), (120001, True)])
def test_20_deadline_margin(env, spawns, monkeypatch, margin, spawned):
    rv = make_review(env)
    to_admitted(env, rv)
    now = 5_000_000_000_000
    monkeypatch.setattr(br, "_now_ms", lambda: now)
    set_row(env, rv.review, hard_deadline_ms=now + margin)
    env.hub.receipts = ["live", "unavailable"]
    result = prepare(env, rv)
    assert bool(spawns) == spawned
    if not spawned:
        assert result == "retired_unknown"
    else:
        assert spawns[0][0][0][-3] != ""  # argv carried the wall clock
        assert int(spawns[0][0][0][-2]) == (margin - 60000) // 1000


def test_21_prompt_altered_after_grant(env, spawns):
    rv = make_review(env)
    to_admitted(env, rv)
    Path(row_of(env, rv.review)["work_dir"], "prompt.txt").write_bytes(b"tampered")
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    assert spawns == []


def test_21_child_wrong_digest(tmp_path, monkeypatch):
    (tmp_path / "prompt.txt").write_bytes(b"prompt")
    called = []
    monkeypatch.setattr(br, "complete_fn", lambda m: called.append(m) or "x")
    monkeypatch.setattr(br, "_tool_inventory", list)
    assert br._child_body(str(tmp_path), 5, "0" * 64) == 5
    assert called == [] and not (tmp_path / "result.json").exists()


def test_22_real_subprocess_reaches_settlement(env, spawns):
    rv = make_review(env)
    env.hub.receipts = ["live", "live", "approved"]
    assert prepare(env, rv) == "approved"
    row = row_of(env, rv.review)
    assert Path(row["work_dir"], "result.json").read_text() == VERDICT_APPROVE
    assert len(spawns) == 1 and len(env.hub.ops_sent) == 1
    (op,) = env.hub.ops_sent[0]
    assert op["op"] == "complete" and op["task"] == rv.review and op["client_op_id"] == row["settle_op_id"]
    kwargs = spawns[0][1]
    assert kwargs["start_new_session"] is True and len(kwargs["pass_fds"]) == 1
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_23_wall_clock_kill(env, spawns, monkeypatch):
    rv = make_review(env)
    to_admitted(env, rv)
    monkeypatch.setattr(br, "_child_argv", fake_child_argv(sleep=30))
    monkeypatch.setattr(br, "_parent_wait_seconds", lambda wall: 3)
    now = int(time.time() * 1000)
    set_row(env, rv.review, hard_deadline_ms=now + 120_001 + 1000)  # wall clock = 61 s; parent waits 3 s
    env.hub.receipts = ["live"]
    started = time.monotonic()
    assert prepare(env, rv) == "retired_unknown"
    assert time.monotonic() - started < 25
    assert not Path(row_of(env, rv.review)["work_dir"], "result.json").exists()


def test_24_child_alarm_without_parent(tmp_path):
    prompt = b"p"
    (tmp_path / "prompt.txt").write_bytes(prompt)
    argv = fake_child_argv(sleep=30)(str(tmp_path), 1, hashlib.sha256(prompt).hexdigest())
    started = time.monotonic()
    proc = subprocess.run(argv, timeout=20, check=False)
    assert proc.returncode == -signal.SIGALRM
    assert time.monotonic() - started < 15
    assert not (tmp_path / "result.json").exists()


def test_25_tool_inventory_nonempty(tmp_path, monkeypatch):
    prompt = b"p"
    (tmp_path / "prompt.txt").write_bytes(prompt)
    called = []
    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions",
                        lambda **kw: [{"type": "function", "function": {"name": "terminal"}}])
    monkeypatch.setattr(br, "complete_fn", lambda m: called.append(m) or "x")
    assert br._child_body(str(tmp_path), 5, hashlib.sha256(prompt).hexdigest()) == 3
    assert called == []


def test_25_real_empty_inventory(monkeypatch):
    for key in br.CHILD_ENV_DROP:
        monkeypatch.delenv(key, raising=False)
    assert br._tool_inventory() == []


def _resp(content, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                                           tool_calls=tool_calls))])


def test_26_production_complete_fn_call_shape(tmp_path, monkeypatch):
    from agent import auxiliary_client as aux
    seen = []

    def fake_call_llm(task, **kw):
        seen.append((task, kw))
        kw["route_info"].update(provider="p-route", model="m-route")
        client = aux.CodexAuxiliaryClient.__new__(aux.CodexAuxiliaryClient)
        return aux._create_with_progress(client, {"model": "m", "messages": kw["messages"]}, task)

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    monkeypatch.setattr(aux, "_create_with_progress", lambda client, kwargs, *a, **k: _resp("ok"))
    monkeypatch.setitem(br._CHILD, "work_dir", str(tmp_path))
    assert br.complete_fn([{"role": "user", "content": "x"}]) == "ok"
    ((task, kw),) = seen
    assert task == "kanban_business_review" and kw["model"] == "class:deep" and kw["tools"] is None
    for key in ("provider", "base_url", "api_key", "api_mode", "extra_body", "extra_headers"):
        assert key not in kw
    assert json.loads((tmp_path / "route.json").read_text()) == {"provider": "p-route", "model": "m-route"}


@pytest.mark.parametrize("failure", ["routing", "non_string"])
def test_26_child_exit_4(tmp_path, monkeypatch, failure):
    from agent import auxiliary_client as aux
    from agent.model_routing import ModelRoutingError
    prompt = b"p"
    (tmp_path / "prompt.txt").write_bytes(prompt)
    monkeypatch.setattr(br, "_tool_inventory", list)

    def fake_call_llm(task, **kw):
        if failure == "routing":
            raise ModelRoutingError("no eligible route")
        client = aux.CodexAuxiliaryClient.__new__(aux.CodexAuxiliaryClient)
        return aux._create_with_progress(client, {"model": "m"}, task)

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    monkeypatch.setattr(aux, "_create_with_progress", lambda c, k, *a, **kw: _resp(["not", "text"]))
    assert br._child_body(str(tmp_path), 5, hashlib.sha256(prompt).hexdigest()) == 4
    assert not (tmp_path / "result.json").exists()


class _Recorder:
    def __init__(self, text="clean text", tool_calls=None):
        self.created = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.text, self.tool_calls = text, tool_calls

    def _create(self, **kwargs):
        self.created.append(kwargs)
        return _resp(self.text, self.tool_calls)


def _routing_offline(monkeypatch, routes, *, extra_body_for=None):
    """Real call_llm + class routing; fake resolved routes whose clients are recorders."""
    from agent import auxiliary_class_routing as acr
    from agent import auxiliary_client as aux
    attempts = []

    def fake_class_request(task, model, kwargs, requirements=None, **rest):
        last = None
        for client, runtime in routes:
            attempts.append(type(client).__name__)
            request = {"model": runtime["model"], "messages": kwargs["messages"]}
            if extra_body_for is client:
                request["extra_body"] = {"tools": [{"type": "function"}]}
            try:
                return aux._create_with_progress(client, request, task)
            except br.ToolInventoryRefused as exc:
                last = exc
        raise last or RuntimeError("no route")

    return attempts, fake_class_request


def _openai_recorder(text="clean text", tool_calls=None):
    import openai
    client = openai.OpenAI.__new__(openai.OpenAI)
    rec = _Recorder(text, tool_calls)
    client.__dict__["chat"] = rec.chat
    return client, rec


def _run_complete(monkeypatch, routes, extra_body_for=None, tmp_path=None):
    from agent import auxiliary_client as aux
    _attempts, class_request = _routing_offline(monkeypatch, routes, extra_body_for=extra_body_for)

    def fake_call_llm(task, *, model, messages, tools, timeout, route_info, **kw):
        assert model == "class:deep" and tools is None
        resp = class_request(task, model, {"messages": messages})
        route_info.update(provider="fake", model=routes[-1][1]["model"])
        return resp

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    monkeypatch.setattr(aux, "_create_with_progress_once",
                        lambda client, kwargs, *a, **k: client.chat.completions.create(**kwargs))
    if tmp_path is not None:
        monkeypatch.setitem(br._CHILD, "work_dir", str(tmp_path))
    return br.complete_fn([{"role": "user", "content": "review"}])


def test_26a_i_extra_body_tools_refused(monkeypatch):
    client, rec = _openai_recorder()
    with pytest.raises(br.ToolInventoryRefused):
        _run_complete(monkeypatch, [(client, {"model": "route-a"})], extra_body_for=client)
    assert rec.created == []


def test_26a_i_real_custom_endpoint_extra_body(monkeypatch):
    """The real kwargs builder merges a destination's configured extra_body; the guard sees the final dict."""
    from agent import auxiliary_client as aux
    import inspect
    assert "extra_body" in inspect.getsource(aux._with_custom_endpoint_extra_body)
    client, rec = _openai_recorder()
    allowed = br._allowed_client_types()
    with pytest.raises(br.ToolInventoryRefused):
        br._check_request(client, {"model": "m", "extra_body": {"tool_choice": "auto"}}, allowed)
    assert rec.created == []


def test_26a_ii_second_clean_route_wins(monkeypatch):
    dirty, dirty_rec = _openai_recorder("dirty")
    clean, clean_rec = _openai_recorder("clean text")
    assert _run_complete(monkeypatch, [(dirty, {"model": "a"}), (clean, {"model": "b"})],
                         extra_body_for=dirty) == "clean text"
    assert dirty_rec.created == []
    (kw,) = clean_rec.created
    assert not any(k in kw for k in br.FORBIDDEN_KWARGS)


def test_26a_iii_unguarded_text(monkeypatch):
    from agent import auxiliary_client as aux
    monkeypatch.setattr(aux, "call_llm", lambda task, **kw: _resp("smuggled"))
    with pytest.raises(br.ToolInventoryRefused):
        br.complete_fn([{"role": "user", "content": "x"}])


def test_26a_iv_tool_calls_in_response(monkeypatch):
    client, rec = _openai_recorder("text", tool_calls=[{"id": "1"}])
    with pytest.raises(br.ToolInventoryRefused):
        _run_complete(monkeypatch, [(client, {"model": "a"})])
    assert len(rec.created) == 1


def test_26a_v_refused_client_types(monkeypatch):
    import openai
    from agent.acp_client import ACPClient

    class Sub(openai.OpenAI):
        pass

    for kind in (ACPClient, Sub, _Recorder):
        client = kind.__new__(kind)
        rec = _Recorder()
        client.__dict__["chat"] = rec.chat
        with pytest.raises(br.ToolInventoryRefused):
            _run_complete(monkeypatch, [(client, {"model": "a"})])
        assert rec.created == []


def test_26a_vi_allowed_types():
    import openai
    from agent import auxiliary_client as aux
    from agent.gemini_native_adapter import GeminiNativeClient
    allowed = br._allowed_client_types()
    for kind in (openai.OpenAI, aux.CodexAuxiliaryClient, aux.AnthropicAuxiliaryClient,
                 aux.BedrockAuxiliaryClient, GeminiNativeClient):
        br._check_request(kind.__new__(kind), {"model": "m", "messages": []}, allowed)
    for key in br.FORBIDDEN_KWARGS:
        with pytest.raises(br.ToolInventoryRefused):
            br._check_request(openai.OpenAI.__new__(openai.OpenAI), {key: []}, allowed)
    with pytest.raises(br.ToolInventoryRefused):
        br._check_request(openai.OpenAI.__new__(openai.OpenAI), {"stream": True}, allowed)


def _hold_child_lock(env, rv):
    import fcntl
    work = Path(row_of(env, rv.review)["work_dir"])
    fd = os.open(str(work / "child.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_27_executing_recovery(env, spawns):
    rv = make_review(env)
    to_admitted(env, rv)
    set_row(env, rv.review, local_state="executing")
    fd = _hold_child_lock(env, rv)
    try:
        env.hub.receipts = ["live"]
        assert prepare(env, rv) == "unavailable"
    finally:
        os.close(fd)
    assert spawns == [] and row_of(env, rv.review)["local_state"] == "executing"
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    rv2 = make_review(env)
    to_admitted(env, rv2)
    set_row(env, rv2.review, local_state="executing")
    Path(row_of(env, rv2.review)["work_dir"], "result.json").write_text(VERDICT_APPROVE)
    env.hub.receipts = ["live", "approved"]
    assert prepare(env, rv2) == "approved"
    assert spawns == [] and len(env.hub.ops_sent) == 1


def test_28_popen_raises(env, monkeypatch):
    import fcntl
    rv = make_review(env)
    to_admitted(env, rv)

    def boom(*a, **k):
        raise OSError("exec failed")
    monkeypatch.setattr(br.subprocess, "Popen", boom)
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    fd = os.open(str(Path(row_of(env, rv.review)["work_dir"], "child.lock")), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released
    os.close(fd)


def _subprocess_script(env, body):
    return (
        "import sys, os, json\n"
        f"sys.path.insert(0, {REPO!r})\n"
        f"os.environ['HERMES_HOME'] = {str(env.home)!r}\n"
        f"os.environ['HERMES_KANBAN_HOME'] = {str(env.home)!r}\n"
        "from hermes_cli import kanban_business_review as br\n"
        "from hermes_cli import kanban_db_connect as kbc\n"
        + body
    )


def test_28a_inherited_lock_survives_parent(env):
    import fcntl
    rv = make_review(env)
    to_admitted(env, rv)
    row = row_of(env, rv.review)
    work = row["work_dir"]
    gate = Path(work) / "gate"
    child_script = (
        "import sys, time, os, json\n"
        "w = sys.argv[1]\n"
        "while not os.path.exists(os.path.join(w, 'gate')): time.sleep(0.05)\n"
        f"open(os.path.join(w, 'result.json.tmp'), 'w').write({VERDICT_APPROVE!r})\n"
        "os.replace(os.path.join(w, 'result.json.tmp'), os.path.join(w, 'result.json'))\n"
    )
    parent = _subprocess_script(env, (
        "import subprocess, fcntl\n"
        f"cs = {child_script!r}\n"
        "br._child_argv = lambda w, s, d: [sys.executable, '-c', cs, w]\n"
        "class Exit(BaseException): pass\n"
        "real = subprocess.Popen\n"
        "def popen(*a, **k):\n"
        "    p = real(*a, **k)\n"
        "    os.write(1, b'spawned\\n')\n"
        "    return p\n"
        "br.subprocess.Popen = popen\n"
        "def no_wait(self, timeout=None):\n"
        "    os._exit(0)\n"
        "real.wait = no_wait  # br.subprocess is this module, so patch the real class\n"
        "class Hub:\n"
        "    def _call(self, m, p, b=None):\n"
        "        return {'state': 'admitted', 'receipt': None, 'restart_permitted': False,"
        " 'settlement_permitted': False, 'resend_permitted': False}\n"
        "conn = kbc.connect()\n"
        f"br.prepare(conn, Hub(), {env.board!r}, {rv.review!r})\n"
    ))
    log = Path(work).parent / "parent.log"
    with open(log, "wb") as sink:  # a pipe would stay open in the surviving child
        out = subprocess.run([sys.executable, "-c", parent], stdout=sink, stderr=subprocess.STDOUT, timeout=60, check=False)
    assert out.returncode == 0 and b"spawned" in log.read_bytes(), log.read_bytes()[-2000:]
    assert row_of(env, rv.review)["local_state"] == "executing"
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "unavailable"  # child still holds the inherited lock
    gate.write_text("go")
    fd = os.open(str(Path(work) / "child.lock"), os.O_RDWR)
    deadline = time.monotonic() + 20
    while True:  # wait for the child to exit and release its inherited lock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            assert time.monotonic() < deadline
            threading.Event().wait(0.05)
    os.close(fd)
    env.hub.receipts = ["live", "approved"]
    assert prepare(env, rv) == "approved"
    assert len(env.hub.ops_sent) == 1


def test_28b_crash_between_executing_and_popen(env, monkeypatch):
    rv = make_review(env)
    to_admitted(env, rv)
    crash = _subprocess_script(env, (
        "def popen(*a, **k):\n"
        "    os._exit(9)\n"
        "br.subprocess.Popen = popen\n"
        "class Hub:\n"
        "    def _call(self, m, p, b=None):\n"
        "        return {'state': 'admitted', 'receipt': None, 'restart_permitted': False,"
        " 'settlement_permitted': False, 'resend_permitted': False}\n"
        "conn = kbc.connect()\n"
        f"br.prepare(conn, Hub(), {env.board!r}, {rv.review!r})\n"
    ))
    out = subprocess.run([sys.executable, "-c", crash], capture_output=True, timeout=60, check=False)
    assert out.returncode == 9, out.stderr.decode()[-2000:]
    assert row_of(env, rv.review)["local_state"] == "executing"
    called = []
    monkeypatch.setattr(br, "complete_fn", lambda m: called.append(m) or "x")
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    assert called == []


def test_29_two_threads_one_spawn(env, monkeypatch):
    rv = make_review(env)
    to_admitted(env, rv)
    monkeypatch.setattr(br, "_child_argv", fake_child_argv(sleep=1.5))
    seen = []
    real = subprocess.Popen
    monkeypatch.setattr(br.subprocess, "Popen", lambda *a, **k: seen.append(1) or real(*a, **k))
    env.hub.receipt_default = "live"
    results = []

    def run():
        conn = kbc.connect()
        try:
            results.append(br.prepare(conn, env.hub, env.board, rv.review))
        finally:
            conn.close()
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(seen) == 1
    assert "unavailable" in results


BAD_OUTPUTS = {
    "non_json": b"approve",
    "trailing": VERDICT_APPROVE.encode() + b" x",
    "list_root": b"[]",
    "extra_key": json.dumps({"verdict": "approve", "summary": "s", "findings": [], "x": 1}).encode(),
    "repeated_verdict": b'{"verdict":"approve","verdict":"approve","summary":"s","findings":[]}',
    "repeated_in_finding": b'{"verdict":"request_changes","summary":"s","findings":[{"title":"t","title":"u","body":"b"}]}',
    "nan": b'{"verdict":"approve","summary":"s","findings":[],"n":NaN}',
    "nan_value": b'{"verdict":"approve","summary":NaN,"findings":[]}',
    "too_big": json.dumps({"verdict": "approve", "summary": "s" * 3000, "findings": []}).encode()
    + b" " * (64 * 1024),
    "invalid_utf8": b'{"verdict":"approve","summary":"\xff","findings":[]}',
    "maybe": json.dumps({"verdict": "maybe", "summary": "s", "findings": []}).encode(),
    "approve_with_finding": json.dumps({"verdict": "approve", "summary": "s",
                                        "findings": [{"title": "t", "body": "b"}]}).encode(),
    "changes_without": json.dumps({"verdict": "request_changes", "summary": "s", "findings": []}).encode(),
    "blank_summary": json.dumps({"verdict": "approve", "summary": "  ", "findings": []}).encode(),
    "long_summary": json.dumps({"verdict": "approve", "summary": "s" * 4001, "findings": []}).encode(),
    "41_findings": json.dumps({"verdict": "request_changes", "summary": "s",
                               "findings": [{"title": "t", "body": "b"}] * 41}).encode(),
    "finding_extra": json.dumps({"verdict": "request_changes", "summary": "s",
                                 "findings": [{"title": "t", "body": "b", "x": "y"}]}).encode(),
    "finding_not_object": json.dumps({"verdict": "request_changes", "summary": "s", "findings": ["t"]}).encode(),
    "missing_verdict": json.dumps({"summary": "s", "findings": []}).encode(),
    "missing_summary": json.dumps({"verdict": "approve", "findings": []}).encode(),
    "missing_findings": json.dumps({"verdict": "approve", "summary": "s"}).encode(),
    "summary_not_string": json.dumps({"verdict": "approve", "summary": 5, "findings": []}).encode(),
    "findings_not_list": json.dumps({"verdict": "approve", "summary": "s", "findings": {}}).encode(),
}
for _field in ("title", "body"):
    for _name, _value in (("missing", None), ("blank", " "), ("non_string", 3), ("long", "x" * 2001)):
        _finding = {"title": "t", "body": "b"}
        if _value is None:
            del _finding[_field]
        else:
            _finding[_field] = _value
        BAD_OUTPUTS[f"{_field}_{_name}"] = json.dumps({"verdict": "request_changes", "summary": "s",
                                                       "findings": [_finding]}).encode()


@pytest.mark.parametrize("name", sorted(BAD_OUTPUTS))
def test_30_bad_output_sends_no_op(env, name):
    assert br.parse_verdict(BAD_OUTPUTS[name]) is None
    rv = make_review(env)
    to_admitted(env, rv)
    set_row(env, rv.review, local_state="executing")
    Path(row_of(env, rv.review)["work_dir"], "result.json").write_bytes(BAD_OUTPUTS[name])
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    assert env.hub.ops_sent == []


def test_30_valid_outputs_parse():
    assert br.parse_verdict(VERDICT_APPROVE.encode())["verdict"] == "approve"
    assert br.parse_verdict(b"  " + VERDICT_CHANGES.encode() + b"\n\t ")["verdict"] == "request_changes"
    exact = json.dumps({"verdict": "approve", "summary": "s" * 4000, "findings": []}).encode()
    assert br.parse_verdict(exact) is not None


# ----------------------------------------------------------- 31-37. settlement

def _held(env, rv, text=VERDICT_APPROVE):
    to_admitted(env, rv)
    set_row(env, rv.review, local_state="executing")
    Path(row_of(env, rv.review)["work_dir"], "result.json").write_text(text)


def test_31_approve_settlement(env):
    rv = make_review(env)
    _held(env, rv)
    env.hub.receipts = ["live", "live", "live"]
    assert prepare(env, rv) == "retired_unknown"
    assert len(env.hub.ops_sent) == 2 and env.hub.ops_sent[0] == env.hub.ops_sent[1]
    (op,) = env.hub.ops_sent[0]
    row = row_of(env, rv.review)
    assert op["op"] == "complete" and op["summary"] == op["result"] == "Looks right \u00e9"
    assert (op["task"], op["run_uid"], op["operation_uid"]) == (rv.review, row["run_uid"], row["operation_uid"])
    assert op["capability_digest"] == br.EMPTY_CAPABILITY_DIGEST and "reason" not in op and "force" not in op
    body = {k: v for k, v in op.items() if k not in ("client_op_id", "payload_hash")}
    assert op["payload_hash"] == hashlib.sha256(br.canonical(body)).hexdigest()
    rv2 = make_review(env)
    _held(env, rv2)
    env.hub.ops_replies = [{"results": [{"status": "rejected", "reason": "x"}]}]
    env.hub.receipts = ["live", "approved"]
    assert prepare(env, rv2) == "approved"


def test_32_request_changes(env):
    rv = make_review(env)
    _held(env, rv, VERDICT_CHANGES)
    env.hub.receipts = ["live", "repair"]
    assert prepare(env, rv) == "repair"
    (op,) = env.hub.ops_sent[0]
    assert op["op"] == "request_changes" and op["reason"] == op["summary"] == "Fix it"
    assert "findings" not in op


def test_33_ops_raises(env):
    rv = make_review(env)
    _held(env, rv)
    env.hub.ops_replies = [HubError(409, "stream_legacy_fenced"), HubError(409, "stream_legacy_fenced")]
    env.hub.receipts = ["live", "live", "live"]
    assert prepare(env, rv) == "retired_unknown"
    assert len(env.hub.ops_sent) == 2


def test_34_deadline(env, monkeypatch):
    rv = make_review(env)
    _held(env, rv)
    deadline = row_of(env, rv.review)["hard_deadline_ms"]
    monkeypatch.setattr(br, "_now_ms", lambda: deadline)
    env.hub.receipts = ["live"]
    assert prepare(env, rv) == "retired_unknown"
    assert env.hub.ops_sent == []
    rv2 = make_review(env)
    _held(env, rv2)
    deadline2 = row_of(env, rv2.review)["hard_deadline_ms"]
    monkeypatch.setattr(br, "_now_ms", lambda: deadline2 - 1)
    real_ops = env.hub.ops

    def ops_then_late(board, ops):
        monkeypatch.setattr(br, "_now_ms", lambda: deadline2)
        return real_ops(board, ops)
    monkeypatch.setattr(env.hub, "ops", ops_then_late)
    env.hub.receipts = ["live", "live"]
    assert prepare(env, rv2) == "retired_unknown"
    assert len(env.hub.ops_sent) == 1


def test_35_settle_sent_resend(env):
    rv = make_review(env)
    _held(env, rv)
    env.hub.ops_replies = [OSError("lost")]
    env.hub.receipts = ["live", "unavailable"]
    assert prepare(env, rv) == "unavailable"
    row = row_of(env, rv.review)
    assert row["local_state"] == "settle_sent" and row["settle_attempts"] == 1
    env.hub.receipts = ["live", "approved"]
    assert prepare(env, rv) == "approved"
    assert len(env.hub.ops_sent) == 2 and env.hub.ops_sent[0] == env.hub.ops_sent[1]
    rv2 = make_review(env)
    _held(env, rv2)
    env.hub.receipts = ["live", "unavailable"]
    prepare(env, rv2)
    set_row(env, rv2.review, settle_attempts=2)
    sent = len(env.hub.ops_sent)
    env.hub.receipts = ["live"]
    assert prepare(env, rv2) == "retired_unknown"
    assert len(env.hub.ops_sent) == sent


@pytest.mark.parametrize("cls", ["approved", "repair", "closed"])
def test_36_done_rows(env, cls):
    rv = make_review(env)
    _held(env, rv, VERDICT_CHANGES if cls == "repair" else VERDICT_APPROVE)
    env.hub.receipts = ["live", cls]
    assert prepare(env, rv) == cls
    calls = len(env.hub.calls)
    assert prepare(env, rv) == cls
    assert len(env.hub.calls) == calls
    if cls == "repair":
        with env.conn:
            env.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (rv.repair,))  # repair finished
            env.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (rv.review,))
        ok, why = kb.request_review(env.conn, rv.review, summary="again", metadata=rv.meta, force=True,
                                    with_reason=True)
        assert ok, why
        assert prepare(env, rv) == "repair"
        assert len(env.hub.calls) == calls


def test_37_one_row_per_task_never_deleted(env):
    rv = make_review(env)
    _refused(env, rv)
    for _ in range(5):
        env.hub.receipts = ["absent", "absent"]
        env.hub.lease_replies = [{"status": "rejected"}]
        prepare(env, rv)
        rows = [r for r in sidecar_rows(env) if r["task_id"] == rv.review]
        assert len(rows) == 1
    assert row_of(env, rv.review)["local_state"] == "retired_unknown"
    source = Path(br.__file__).read_text()
    assert "DELETE" not in source.upper().replace("DELETED", "")
