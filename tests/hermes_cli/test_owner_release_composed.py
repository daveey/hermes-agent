"""Composed owner-release path over ONE real local wrangler Board (Durable Objects).

native CLI -> inherited socket -> native parent -> REAL ``kanban_sync_client.Hub`` HTTP
-> wrangler Board DO -> real HOME SQLite replica (pull/queue/consume/final/materialize)
-> native result -> a distinct executor SQLite replica (private certificate + import).

Nothing here substitutes a Hub, a certificate or a success value. Fixture identities
are enrolled gateways (admin enrol over HTTP); every principal is authenticated by its
own bearer key, so the Board binds origin/HOME/executor by transport, not by a test seam.

Needs a DISPOSABLE local wrangler (never a live Board)::

    HERMES_COMPOSED_BOARD_URL=http://127.0.0.1:8794 HERMES_COMPOSED_REQUIRED=1 \
        bash scripts/run_tests.sh -j 1 tests/hermes_cli/test_owner_release_composed.py \
        tests/hermes_cli/test_owner_release_composed_controls.py

Use one per-file worker: the native fixture binds exact gateway names; running
these two files concurrently would rotate each other's disposable enrollment keys.

The server must run with ADMIN_KEY_SHA256=sha256("review-local-admin"). Without it the
module skips (HERMES_COMPOSED_REQUIRED=1 turns the skip into a failure).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_origin, kanban_sync_client as ksc
from tests.hermes_cli import test_native_owner_release_cli as native_helpers
from tests.tools import test_kanban_worker_created_chats as chats

URL = os.environ.get("HERMES_COMPOSED_BOARD_URL", "http://127.0.0.1:8794").rstrip("/")
ADMIN = os.environ.get("HERMES_COMPOSED_ADMIN_KEY", "review-local-admin")
BOARD = "default"  # the native CLI resolves the fixture HOME's current board
turn = native_helpers.turn
HOME_NAME = "composed-home"
SPEND_HOME_PRIVATE_PKCS8 = "MC4CAQAwBQYDK2VwBCIEINfoNQnygSH1fC62cjK28oWZwUKuCJlEwGdb7KmnoG/9"  # Board repo's TEST-ONLY fixture key
SPEND_AUDIENCE = "hub.dbloom.in/episode-run-allowance-v1"
_REGISTER = ("version", "audience", "board", "task", "home", "request_uid", "previous_episode_uid", "episode_uid",
             "producer_task_uid", "holds_clear", "expected_task_version", "issued_at_ms", "expires_at_ms")
_INSTALL = ("version", "audience", "board", "task", "home", "request_uid", "episode_uid", "producer_task_uid",
            "decision_ref", "decision_sha256", "runs", "cap_reason", "holds_clear", "expected_task_version",
            "issued_at_ms", "expires_at_ms")


def install_allowance(home, task, episode, intent_uid):
    """HOME's independent signed finite run allowance, which the DO requires before apply (a precondition, not minted
    by the release path). Signed with the Board repo's test-only HOME key; real signer wiring is a separate gate."""
    import base64
    import time
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_der_private_key(base64.b64decode(SPEND_HOME_PRIVATE_PKCS8), None)
    proof = home.hub.owner_release_evidence(BOARD, intent_uid)
    for kind in ("register", "install"):
        row = home.hub.row_evidence(BOARD, "tasks", task)["row"]
        now = int(time.time() * 1000)
        payload = {"version": 1, "audience": SPEND_AUDIENCE, "board": BOARD, "task": task, "home": HOME_NAME,
                   "request_uid": "signed-" + uuid.uuid4().hex, "episode_uid": episode, "producer_task_uid": task,
                   "holds_clear": True, "expected_task_version": row["version"], "issued_at_ms": now - 10,
                   "expires_at_ms": now + 30000}
        if kind == "register":
            payload["previous_episode_uid"] = None
        else:
            payload.update(decision_ref="intent:" + intent_uid, decision_sha256=proof["digest"],
                           runs=proof["intent"]["runs"], cap_reason="Existing independent finite fixture decision")
        fields = _REGISTER if kind == "register" else _INSTALL
        domain = "hermes-board-spend-episode-v1" if kind == "register" else "hermes-board-spend-window-v1"
        message = json.dumps([domain, *[payload[f] for f in fields]], separators=(",", ":"), ensure_ascii=False).encode()
        signature = base64.b64encode(key.sign(message)).decode()
        path = f"/v2/boards/{BOARD}/spend-windows" + ("/episodes" if kind == "register" else "")
        reply = home.hub._call("POST", path, {"payload": payload, "kid": "k1", "signature": signature})
        assert reply.get("status") == {"register": "registered", "install": "installed"}[kind], reply
        assert reply["current_episode_uid"] == episode and reply["state"] == {"register": "pending", "install": "active"}[kind], reply


def _raw(path, key=None, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + key} if key else {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


@pytest.fixture(scope="module")
def server():
    parts = urllib.parse.urlsplit(URL)
    assert parts.hostname in {"127.0.0.1", "localhost"}, "composed harness refuses non-local Boards"
    try:
        _raw("/v1/admin/gateways", ADMIN, {"name": "ct-probe-" + uuid.uuid4().hex[:6]})
    except (OSError, urllib.error.URLError) as exc:  # URLError subclasses OSError; HTTPError too
        if os.environ.get("HERMES_COMPOSED_REQUIRED") == "1":
            raise
        pytest.skip(f"no disposable local wrangler Board at {URL}: {exc}")
    return URL


def enroll(prefix, exact=False):
    name = prefix if exact else f"{prefix}-{uuid.uuid4().hex[:8]}"
    code = _raw("/v1/admin/gateways", ADMIN, {"name": name})["code"]
    key = "hmk_" + uuid.uuid4().hex
    _raw("/v1/enroll", body={"gateway": name, "code": code, "key_sha256": hashlib.sha256(key.encode()).hexdigest()})
    return ksc.Identity(url=URL, key=key, gateway=name)


class Party:
    """An authenticated gateway with its own SQLite replica."""
    def __init__(self, prefix, tmp_path, primary=False, exact=False):
        self.ident = enroll(prefix, exact=exact)
        self.name = self.ident.gateway
        self.path = tmp_path / f"{prefix}.db"
        self.hub = ksc.Hub(self.ident)
        self.conn = kbc.connect(self.path)
        ksc.ensure_identity(self.conn, BOARD, self.name)
        if primary:
            sync.set_mode(self.conn, "primary")

    def pull(self):
        while True:
            got = ksc.pull(self.conn, self.hub, BOARD, observers=False)
            if not got:
                return

    def resync(self):
        return ksc.resync(self.conn, self.hub, BOARD, observers=False)

    def close(self):
        self.hub.close()
        self.conn.close()


_clock = [0]


def stamp(who):
    _clock[0] += 1
    return f"{int(__import__('time').time() * 1000) + _clock[0]:013d}-0000-{who}"


def seed_stopped_task(home, origin_ident, session):
    """HOME creates the card; a real lease/retain stops it; ORIGIN links its native chat."""
    with pytest.MonkeyPatch.context() as scoped:  # HOME stamps its own gateway name, not the origin's "here"
        scoped.setattr(kanban_origin, "gateway_names", lambda: (home.name, {home.name}))
        task, episode = _home_stop(home)
    return _link_origin(home, origin_ident, session, task, episode)


def _home_stop(home, task=None, reason="scope_stopped", refused=False):
    # HOME acts through its OWN primary SQLite board: real create/push, then the real retain command ledger
    # (reserve -> frozen wire -> Board retain -> independent confirmation -> projection), not a bare Hub op.
    from hermes_cli import kanban_retain_transport as retain
    conn = home.conn
    if task is None:
        task = kb.create_task(conn, title="composed owner release", assignee="default")
        pushed = ksc.push(conn, home.hub, BOARD, home.name, primary=True)
        assert pushed.rejected == 0, pushed
    with kbc.write_txn(conn):
        rid = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'default', 'running', 1)",
                           (task,)).lastrowid
        conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (rid, task))
        conn.execute("DELETE FROM kanban_sync_outbox")  # claim/run rows are synthesised by the lease grant below
    run = sync.run_uid(conn, rid)
    assert isinstance(run, str) and run
    grant = home.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run, "ttl_s": 60})
    assert grant["status"] == "applied", grant
    if refused:
        bound = "bound-" + uuid.uuid4().hex
        value = home.hub.ops(BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert", "table": "task_events",
            "uid": bound, "fields": {"task_id": task, "run_uid": run, "kind": "worker_bound",
            "payload": json.dumps({"pid": 1, "claim_lock": "fixture-only-no-os-probe"}), "created_at": 1001},
            "hlc": stamp(home.name)}])["results"][0]
        assert value["status"] == "applied", value
        source = "refusal-" + uuid.uuid4().hex
        value = home.hub.ops(BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert", "table": "task_events",
            "uid": source, "fields": {"task_id": task, "run_uid": run, "kind": "worker_result",
            "payload": json.dumps({"failed": True, "failure_reason": "content_policy_blocked", "pid": 1,
                                   "claim_lock": "fixture-only-no-os-probe", "failure_retryable": False, "exit_code": 1}), "created_at": 1001},
            "hlc": stamp(home.name)}])["results"][0]
        assert value["status"] == "applied", value
    with kbc.write_txn(conn):
        owned = retain.reserve(conn, task, run, {"version": 1, "summary": "incomplete", "reason": reason,
                                                 "next_step": "owner decides", "release_condition": "scoped release",
                                                 "evidence": ["composed fixture"]}, None)
    result = retain.confirm(conn, home.hub, BOARD, run)
    retain.project(conn, home.hub, BOARD, result)
    assert owned["run_uid"] == run
    home.pull()
    stops = [{"uid": result["episode_uid"]}]
    return task, stops[0]["uid"]


def seed_stacked_task(home, origin_ident, session):
    """Two real retained runs, separated only by the supported legacy promotion.

    Promotion is ordinary history, NOT a private capability release receipt. Use
    real elapsed seconds so the subsequent server stop sorts after that history.
    """
    import time
    from tests.waiting import wait_until
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(kanban_origin, "gateway_names", lambda: (home.name, {home.name}))
        task, old = _home_stop(home, reason="capability_unavailable")
        stop = home.hub.row_evidence(BOARD, "task_events", old)["row"]
        assert json.loads(stop["payload"])["cause"] == "capability_unavailable"
        wait_until(lambda: int(time.time()) > stop["created_at"], timeout=3)
        promoted_at = int(time.time())
        promotion = "legacy-promotion-" + uuid.uuid4().hex
        results = home.hub.ops(BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert",
            "table": "task_events", "uid": promotion, "fields": {"task_id": task,
            "kind": "promoted_manual", "payload": json.dumps({"reason": "legacy promotion without release receipt"}),
            "created_at": promoted_at}, "hlc": stamp(home.name)},
            {"client_op_id": str(uuid.uuid4()), "op": "upsert", "table": "tasks", "uid": task,
             "fields": {"status": "ready"}, "hlc": stamp(home.name)}])["results"]
        assert all(r["status"] == "applied" for r in results), results
        assert home.hub.row_evidence(BOARD, "task_events", promotion)["row"]["kind"] == "promoted_manual"
        wait_until(lambda: int(time.time()) > promoted_at, timeout=3)
        home.pull()
        task, current = _home_stop(home, task=task)
    assert old != current
    assert not release_receipts(home, task)
    _link_origin(home, origin_ident, session, task, current)
    return task, current, old


def release_receipts(home, task):
    home.pull()
    return [json.loads(r[0]) for r in home.conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='stop_released' ORDER BY id", (task,))]


def event_inventory(home, task):
    # Pull the real canonical feed, including any new duplicate release/budget rows.
    home.pull()
    return [tuple(r) for r in home.conn.execute(
        "SELECT uid,kind,payload FROM task_events WHERE task_id=? ORDER BY uid", (task,))]


def _link_origin(home, origin_ident, session, task, episode):
    link = "link-" + uuid.uuid4().hex[:8]
    linked = ksc.Hub(origin_ident)
    try:
        applied = linked.ops(BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert", "table": "task_chat_links",
                                      "uid": link, "fields": {"task_id": task, "chat_gateway": origin_ident.gateway,
                                                              "chat_profile": "default", "chat_session": session,
                                                              "kind": "created", "released": 0, "created_at": 1000},
                                      "hlc": stamp(origin_ident.gateway)}])["results"]
    finally:
        linked.close()
    assert applied[0]["status"] == "applied", applied
    return task, episode


def release_cli(native, task, episode, *extra):
    import subprocess
    import sys
    from tools import origin_cli_bridge as bridge
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=True)
    env["PYTHONPATH"] = os.getcwd()
    env[bridge.ENV_KEY] = native.locator
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "release", task, "--episode", episode,
                           "--runs", "1", "--note", "complete the exact original acceptance", "--json", *extra],
                          env=env, pass_fds=(native.child_fd,), capture_output=True, text=True, encoding="utf-8",
                          timeout=60, check=False)


@pytest.fixture
def origin(turn, server, monkeypatch, tmp_path):
    """Native origin parent speaking through the REAL Hub as an enrolled gateway."""
    # The shared native `turn` fixture hard-binds the live lease to gateway "here"; enrol that exact name
    # (re-enrolment rotates its key on the disposable server) so the Board authenticates the same principal.
    ident = enroll("here", exact=True)
    monkeypatch.setattr(ksc, "load_identity", lambda *a, **k: ident)
    # GATE, declared: the Board source deliberately does not advertise owner_release:1 yet (no capability
    # advertisement before ship). Only the native parent's advertisement READ is overlaid; every route,
    # authorization and DO check behind it is the real server's.
    real_capability = ksc.Hub.decision_retain_capability
    monkeypatch.setattr(ksc.Hub, "decision_retain_capability",
                        lambda self, board: {**real_capability(self, board), "owner_release": 1})
    native = native_helpers.launch(monkeypatch)
    try:
        yield ident, native
    finally:
        native_helpers.close(native)


@pytest.fixture
def parties(server, tmp_path):
    # HOME's name must match a SPEND_HOME_PUBLIC_KEYS entry on the disposable Board (see start_wrangler.sh).
    home, executor = Party(HOME_NAME, tmp_path, primary=True, exact=True), Party("ct-exec", tmp_path)
    try:
        yield home, executor
    finally:
        home.close()
        executor.close()


def diagnose(home, task, episode, intent_uid):
    """Failure aid only: HOME's local scope evidence against the DO's winning consumption coverage."""
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_release_certificate as cert
    receipt = cert.validate(home.hub.owner_release_consumption(BOARD, intent_uid), board=BOARD, task=task, intent_uid=intent_uid)
    local = renewal.scope_evidence(home.conn, task, episode)
    return {"covered": sorted(cert.coverage(receipt)), "local": sorted(renewal.key(e) for e in local),
            "local_kinds": [(e["kind"], e["payload"][:160]) for e in local],
            "events": [(r[0], r[1], (r[2] or "")[:120]) for r in home.conn.execute(
                "SELECT uid,kind,payload FROM task_events WHERE task_id=?", (task,))]}


def home_tick(home, monkeypatch, rounds=1):
    """The real HOME entry point, authenticating as HOME through the real Hub."""
    from hermes_cli import kanban_release_home as release_home
    # HOME is a different gateway from the native origin sharing this pytest process: HOME's identity and
    # stamped name apply for the duration of the HOME tick only, then the origin's "here" is restored.
    with monkeypatch.context() as scoped:
        scoped.setattr(ksc, "load_identity", lambda *a, **k: home.ident)
        scoped.setattr(kanban_origin, "gateway_names", lambda: (home.name, {home.name}))

        for _ in range(rounds):
            home.pull()
            release_home.tick(home.conn, home.conn, home.name, BOARD)


def stage(native_conn_stage="SELECT stage FROM native_owner_release_intents"):
    with kbc.connect_closing() as conn:
        return [r[0] for r in conn.execute(native_conn_stage)]


def test_wrong_caller_controls_never_reach_authorization(origin, parties, monkeypatch):
    home, executor = parties
    ident, native = origin
    # Card is linked to a DIFFERENT chat session: the real native caller is not the owner.
    task, episode = seed_stopped_task(home, ident, "someone-elses-chat")
    first = release_cli(native, task, episode)
    refused = json.loads(first.stdout)
    assert first.returncode == 2 and not refused["ok"], (first.stdout, first.stderr)
    assert "release_read" not in refused and refused.get("release_stage") != "read_first_pending"
    # Another authenticated gateway cannot post the exact intent the native actor would freeze.
    forged = {"version": 1, "intent_uid": str(uuid.uuid4()), "board": BOARD, "task_id": task, "home": home.name,
              "episode": episode, "capability_episodes": [], "runs": 1, "note": "forged",
              "actor": {"gateway": ident.gateway, "profile": "default", "session": chats.CHAT}}
    from hermes_cli import kanban_release_intent as intent
    outsider = ksc.Hub(executor.ident)
    try:
        result = outsider.ops(BOARD, [{"client_op_id": forged["intent_uid"], "op": "authorize_owner_release", "task": task,
                                       "intent": forged, "digest": intent.digest(forged)}])["results"][0]
        assert result["status"] == "rejected", result
        with pytest.raises(ksc.HubError):
            outsider.owner_release_evidence(BOARD, forged["intent_uid"])
    finally:
        outsider.close()


def applied(home, intent_uid):
    return bool(home.conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_release_applied_receipts'").fetchone()
                and home.conn.execute("SELECT 1 FROM owner_release_applied_receipts WHERE intent_uid=?", (intent_uid,)).fetchone())


def authorize_and_drive_home(origin, parties, monkeypatch, *, stacked=False, select=True, governed=True):
    home, executor = parties
    ident, native = origin
    if stacked:
        task, episode, old = seed_stacked_task(home, ident, chats.CHAT)
    else:
        task, episode = seed_stopped_task(home, ident, chats.CHAT)
        old = None
    extra = ("--capability-episode", old) if old and select else ()
    # 1. real CLI -> parent -> real Hub -> DO authorization, read-first challenge, then publication.
    first = release_cli(native, task, episode, *extra)
    assert first.returncode == 0, (first.stdout, first.stderr)
    nonce = json.loads(first.stdout)["release_read"]["nonce"]
    ack = release_cli(native, task, episode, "--seen-release", nonce, *extra)
    assert ack.returncode == 0, (ack.stdout, ack.stderr)
    published = json.loads(ack.stdout)
    assert published["publication_stage"] == "published", published
    intent_uid = published["intent_uid"]
    probe = ksc.Hub(ident)
    try:
        proof = probe.owner_release_evidence(BOARD, intent_uid)
        assert proof["state"] == "authorized"
        assert proof["intent"]["capability_episodes"] == ([old] if old and select else [])
    finally:
        probe.close()
    # Nothing is released yet: HOME has not consumed.
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='stop_released'").fetchone()[0] == 0
    # 2. real HOME: pull carrier, queue, consume, final apply, atomic materialization.
    home.resync()
    if governed:
        install_allowance(home, task, episode, intent_uid)
    for _ in range(80):  # the HOME queue also drains stale releases left on the reused disposable Board
        home_tick(home, monkeypatch)
        if applied(home, intent_uid):
            break
    queue = [tuple(r) for r in home.conn.execute("SELECT stage FROM owner_release_home_queue")]
    omitted = bool(old and not select)
    if not applied(home, intent_uid) and not omitted:
        print("HOME-DIAG", diagnose(home, task, episode, intent_uid))
    if not omitted:
        assert applied(home, intent_uid), queue
    else:
        # An omitted private capability may conservatively prevent final apply,
        # but authorization/consumption must still traverse the real pipeline.
        assert home.hub.owner_release_evidence(BOARD, intent_uid)["state"] == "consumed", queue
    return home, executor, native, task, episode, intent_uid, old


@pytest.mark.parametrize("stacked", [False, True], ids=["scope-only", "selected-historical-capability"])
def test_happy_path_native_to_do_home_native_result_and_executor_import(origin, parties, monkeypatch, stacked):
    home, executor, native, task, episode, intent_uid, old = authorize_and_drive_home(
        origin, parties, monkeypatch, stacked=stacked)
    extra = ("--capability-episode", old) if old else ()
    assert [r["episode"] for r in release_receipts(home, task)] == ([old] if old else []) + [episode]
    # 3. native result reads the private application via the origin's real Hub.
    done = release_cli(native, task, episode, *extra)
    assert done.returncode == 0, (done.stdout, done.stderr)
    value = json.loads(done.stdout)
    assert value["release_stage"] == "applied" and value["execution_clearance"] is False, value
    assert stage() == ["applied"]
    # 4. a separate executor replica pulls the feed; real row_evidence certifies the page.
    executor.resync()
    from hermes_cli import kanban_release_executor as subject
    got = subject.verified_application(executor.conn, intent_uid)
    assert got is not None
    from hermes_cli import kanban_spend_decision as decision
    assert not decision.execution_restricted(executor.conn, task)
    before = event_inventory(home, task)
    task_before = home.hub.row_evidence(BOARD, "tasks", task)["row"]
    certificate = home.hub.owner_release_application(BOARD, intent_uid)
    for _ in range(2):
        replay = release_cli(native, task, episode, *extra)
        assert replay.returncode == 0, (replay.stdout, replay.stderr)
        result = json.loads(replay.stdout)
        assert result["intent_uid"] == intent_uid and result["release_stage"] == "applied", result
        home_tick(home, monkeypatch, rounds=3)
    assert home.hub.owner_release_application(BOARD, intent_uid) == certificate
    assert event_inventory(home, task) == before  # no duplicate release OR budget events
    assert home.hub.row_evidence(BOARD, "tasks", task)["row"] == task_before
    executor.resync()
    assert subject.verified_application(executor.conn, intent_uid) is not None
    assert not decision.execution_restricted(executor.conn, task)


def test_omitted_historical_capability_remains_held(origin, parties, monkeypatch, caplog):
    home, executor, native, task, episode, intent_uid, old = authorize_and_drive_home(
        origin, parties, monkeypatch, stacked=True, select=False)
    from hermes_cli import kanban_spend_decision as decision
    receipts = [r["episode"] for r in release_receipts(home, task)]
    assert not applied(home, intent_uid) and receipts == []
    delivery = home.conn.execute("SELECT delivery_uid FROM owner_release_home_queue WHERE intent_uid=?", (intent_uid,)).fetchone()[0]
    assert any(delivery in r.getMessage() and "restriction_unattributable" in r.getMessage() for r in caplog.records)
    assert old not in receipts
    historical = home.hub.row_evidence(BOARD, "task_events", old)["row"]
    assert historical["kind"] == "spend_budget_triaged"
    assert json.loads(historical["payload"])["cause"] == "capability_unavailable"
    assert decision.execution_restricted(home.conn, task)
    executor.resync()
    assert decision.execution_restricted(executor.conn, task)
    before = event_inventory(home, task)
    for _ in range(2):
        replay = release_cli(native, task, episode)
        assert replay.returncode == 0, (replay.stdout, replay.stderr)
        result = json.loads(replay.stdout)
        assert result["intent_uid"] == intent_uid, result
        assert result.get("execution_clearance") is not True, result
        home_tick(home, monkeypatch, rounds=3)
    assert event_inventory(home, task) == before
    assert old not in [r["episode"] for r in release_receipts(home, task)]
    assert decision.execution_restricted(home.conn, task)
