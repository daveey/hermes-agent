"""Real local Board HTTP/DO -> sync shadow -> pickup -> Core claim composition.

Opt in: B2_M1_LOCAL_BOARD_URL=http://127.0.0.1:8793 bash scripts/run_tests.sh <this file>
Start the recovered Board with `npx wrangler dev --local --ip 127.0.0.1 --port 8793
--var ADMIN_KEY_SHA256:<sha256-of-test-admin-key> --persist-to <disposable directory>` first.
No production enrollment, signer, provider call or permission changes.
"""
import hashlib
import json
import os
import uuid
from contextlib import closing
from urllib.request import Request, urlopen

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as worker
from hermes_cli import kanban_spend_allowance as allowance
from tests.hermes_cli import kanban_sync_support as support

BOARD = "default"
QUOTE = "Remaining work is the portable allowance composition and its refusal proof."


def uid():
    return uuid.uuid4().hex


@pytest.fixture
def composed(tmp_path, monkeypatch):
    url = os.environ.get("B2_M1_LOCAL_BOARD_URL")
    if not url:
        pytest.skip("requires isolated local wrangler Board")
    assert url.startswith("http://127.0.0.1:")
    root = support.kanban_home(tmp_path, monkeypatch)
    # Each fixture owns a separate real Durable Object. A shared "default" Board
    # accumulates every prior test run and makes full snapshots quadratic across reruns.
    monkeypatch.setattr(__name__ + ".BOARD", "allowance-" + uid())
    conns = []
    fixture_label = "fixture:" + uid()
    previous = sync.set_lease_client(None)

    def api(path, payload, key="test-admin-key"):
        with urlopen(Request(url + path, json.dumps(payload).encode(),
                             {"Content-Type": "application/json", "Authorization": "Bearer " + key}), timeout=20) as res:
            return json.load(res)

    class Gateway:
        def __init__(self, prefix):
            self.name = prefix + uid()[:12]
            key = "hmk_" + uid()
            entry = api("/v1/admin/gateways", {"name": self.name})
            api("/v1/enroll", {"gateway": self.name, "code": entry["code"],
                              "key_sha256": hashlib.sha256(key.encode()).hexdigest()}, key)
            api(f"/v1/admin/gateways/{self.name}/machine", {
                "labels": ["linux", "pool:worker", "profile:default", fixture_label], "pickup": "explicit", "max_card_runs": 12})
            self.hub = client.Hub(client.Identity(url, key, self.name))
            self.home = root / self.name
            self.home.mkdir()
            self.path = self.home / "kanban" / "boards" / BOARD / "kanban.db"
            self.path.parent.mkdir(parents=True)
            from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
            (self.home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    pickup: explicit\n" + _managed_yaml())
            self.enter()
            self.conn = kbc.connect(self.path)
            conns.append(self.conn)
            client.ensure_identity(self.conn, BOARD, self.name)
            sync.set_mode(self.conn, "primary")

        def enter(self):
            monkeypatch.setenv("HERMES_HOME", str(self.home))
            monkeypatch.setattr(client, "load_identity", lambda: self.hub.ident)
            sync.set_lease_client(client.HubLeaseClient(self.hub))
            worker.set_registry_fetch(self.hub.machines)

        def round(self):
            self.enter()
            result = client.sync_board(BOARD, self.path, self.hub, self.name)
            assert result.error is None, result
            assert result.push is None or result.push.rejected == 0, result
            return result

        def reopen(self):
            conns.remove(self.conn)
            self.conn.close()
            self.enter()
            self.conn = kbc.connect(self.path)
            conns.append(self.conn)

    try:
        home, pool = Gateway("m1home-"), Gateway("m1pool-")
        home.enter()
        task = kb.create_task(home.conn, title="isolated M1 portable proof", assignee="default",
                              runs_on=["pool:worker", fixture_label])
        home.round()
        # Establish the caused stop through the actual authenticated lifecycle endpoint.

        run = uid()
        lease = home.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run, "ttl_s": 900})
        assert lease["status"] == "applied", lease
        retained = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "retain", "task": task, "run_uid": run,
            "event_uid": uid(), "expected_episode": None, "payload": {"version": 1, "summary": QUOTE,
            "reason": "scope_stopped", "next_step": "Owner decides", "release_condition": "Scoped decision",
            "evidence": ["isolated composition fixture"]}}])
        assert retained["results"][0]["status"] == "applied", retained
        snap = home.hub._call("GET", f"/v2/boards/{BOARD}/snapshot")
        episode = [e for e in snap["task_events"] if e["task_id"] == task and e["kind"] == "spend_budget_triaged"][-1]["uid"]
        review = uid()
        released = home.hub.ops(BOARD, [{"client_op_id": review, "op": "spend_review", "task": task,
            "event_uid": review, "episode": episode, "verdict": "release", "reason": "unfinished proof", "quote": QUOTE}])
        assert released["results"][0]["status"] == "applied", released
        pool.round()
        pool.enter()
        picked = worker.pickup(pool.conn, budget=1)
        with closing(worker.open_replica(pool.conn)) as replica:
            diagnostic = worker._evaluate(pool.conn, replica, pool.name, mode="explicit", limit=1,
                profile_exists=None, full_detail="fixture batch full")
        assert picked == [task], diagnostic
        assert pool.conn.execute("SELECT created_on FROM tasks WHERE id=?", (task,)).fetchone()[0] == home.name
        assert allowance.state(pool.conn, task) == allowance.BOARD
        yield home, pool, task
    finally:
        for conn in conns:
            conn.close()
        sync.set_lease_client(previous)
        worker.set_registry_fetch(None)
        client.reset_board_watch()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_safety_stop_never_renews_or_spends_existing_allowance(composed, lane):
    import time
    home, pool, task = composed
    run = uid()
    granted = home.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run, "ttl_s": 900})
    assert granted["status"] == "applied", granted
    lock = home.name + ":fixture"
    entries = []
    for kind, payload in [("worker_bound", {"pid": 321, "claim_lock": lock}), ("worker_result", {
        "pid": 321, "claim_lock": lock, "exit_code": 1, "failed": True,
        "failure_reason": "content_policy_blocked", "failure_retryable": False})]:
        entries.append({"client_op_id": uid(), "op": "upsert", "table": "task_events", "uid": uid(),
            "hlc": f"{int(time.time() * 1000):013d}-0000-fixture", "fields": {
                "task_id": task, "run_uid": run, "kind": kind, "payload": json.dumps(payload),
                "created_at": int(time.time())}})
    result = home.hub.ops(BOARD, entries)
    assert all(r["status"] == "applied" for r in result["results"]), result
    # The canonical terminal release settles typed refusal evidence; row publication
    # alone is not terminal settlement. Deliberately supply no retry/fallback request.
    terminal = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "release", "task": task,
        "run_uid": run, "outcome": "crashed"}])
    assert terminal["results"][0]["status"] == "applied", terminal
    snapshot = home.hub._call("GET", f"/v2/boards/{BOARD}/snapshot")
    stops = [e for e in snapshot["task_events"] if e["task_id"] == task and e["kind"] == "spend_budget_triaged"
             and json.loads(e["payload"]).get("cause") == "safety_refusal"]
    assert len(stops) == 1
    review = uid()
    denied = home.hub.ops(BOARD, [{"client_op_id": review, "op": "spend_review", "task": task,
        "event_uid": review, "episode": stops[0]["uid"], "verdict": "release", "reason": "fixture forbidden retry", "quote": QUOTE}])
    assert denied["results"][0]["reason"] == "safety_restricted", denied
    pool.round()
    pool.enter()
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:safety") is None
    assert pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]["used"] == 1
    # The one debit preceded the typed refusal. Neither denied reviewer nor later claim debits.


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_human_hold_never_spends_portable_allowance(composed, lane):
    home, pool, task = composed
    if lane == "review":
        out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert out["results"][0]["status"] == "applied", out
        pool.round()
    # Install the hold on the actual authority, not an unpushed local field which
    # the authoritative grant snapshot is allowed to replace.
    import time
    out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "upsert", "table": "tasks", "uid": task,
        "hlc": f"{int(time.time() * 1000):013d}-0000-fixture", "fields": {
            "human_request": json.dumps({"action": "fixture human step"}),
            "status": "blocked", "block_kind": "needs_input"}}])
    assert out["results"][0]["status"] == "applied", out
    pool.round()
    pool.enter()
    held = kb.get_task(pool.conn, task)
    assert held is not None and held.human_request and held.status == "blocked"
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:human") is None
    assert pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]["used"] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_cache_hold_still_refuses_without_board_debit(composed, monkeypatch, lane):
    home, pool, task = composed
    if lane == "review":
        out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert out["results"][0]["status"] == "applied", out
        pool.round()
    pool.enter()
    want = allowance.expected(pool.conn, task)
    assert want is not None
    # Deliberately narrow the replica's cache budget while keeping a recognized
    # portable run basis. Real history accounting must stop before any lease RPC.
    with kbc.write_txn(pool.conn):
        kb._append_event(pool.conn, task, "spend_budget_set", {
            "runs": 10, "runs_basis": allowance.BASIS, "review_uid": want["review_uid"],
            "cache_read_tokens": 1, "cache_read_writer_kind": "human", "writer_kind": "human"})
        run = pool.conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id LIMIT 1", (task,)).fetchone()[0]
        kb._append_event(pool.conn, task, "worker_result", {"usage": {"cache_read_tokens": 10}}, run_id=run)
    from hermes_cli import kanban_spend_budget as budget
    assert budget.spend(pool.conn, task).tokens["cache_read_tokens"] >= 10
    observed = []
    original = pool.hub.leases

    def watch(board, body):
        observed.append(body)
        return original(board, body)

    monkeypatch.setattr(pool.hub, "leases", watch)
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:cache") is None
    assert str(sync.pop_claim_deferral(task)) == "spend_budget_exhausted"
    assert observed == []
    assert pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]["used"] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_snapshot_status_before_budget_refuses_then_recovers(composed, monkeypatch, lane):
    home, pool, task = composed
    if lane == "review":
        out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert out["results"][0]["status"] == "applied", out
    pool.enter()
    # The fixture first proves real foreign pickup. This independent cold rebuild then
    # pauses at the actual HTTP table-page boundary, with status present but no budget.
    with closing(kbc.connect(pool.home / "paged-recovery.db")) as conn:
        client.ensure_identity(conn, BOARD, pool.name)
        sync.set_mode(conn, "primary")
        original = pool.hub.snapshot_page
        observed = []
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task

        def page(board, table, after="", limit=1000):
            if table == "task_events" and not observed:
                partial = kb.get_task(conn, task)
                assert partial is not None and partial.status == lane
                assert allowance.state(conn, task) is None
                assert claim(conn, task, ttl_seconds=900, claimer="fixture:paged") is None
                assert str(sync.pop_claim_deferral(task)) == "reviewer_allowance_changed"
                before = pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]
                assert before["used"] == 0
                observed.append(before)
            response = original(board, table, after, 7 if table == "task_events" else limit)
            if table == "task_events":
                snapshot_events.extend(response["rows"])
            return response

        snapshot_events = []
        monkeypatch.setattr(pool.hub, "snapshot_page", page)
        client.resync(conn, pool.hub, BOARD, observers=False)
        # Actual HTTP pagination must transport canonical arrival order, not UID,
        # writer clock, or mutable feed seq. Local arrival reducers depend on it.
        canonical = [e["uid"] for e in sorted(snapshot_events, key=lambda e: (e["ord"], e["uid"]))]
        assert [e["uid"] for e in snapshot_events] == canonical
        imported = [r[0] for r in conn.execute("SELECT uid FROM task_events ORDER BY id")]
        assert imported == canonical
        assert len(observed) == 1
        assert allowance.state(conn, task) == allowance.BOARD
        result = claim(conn, task, ttl_seconds=900, claimer="fixture:paged")
        from hermes_cli import kanban_spend_decision as decision
        assert result is not None, json.dumps({"reason": str(sync.pop_claim_deferral(task)),
            "pending": decision.pending_episode(conn, task),
            "events": [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (task,))],
            "picked_events": [dict(r) for r in pool.conn.execute("SELECT * FROM task_events WHERE task_id=?", (task,))]}, default=str)
        after = pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]
        assert after["used"] == 1 and after["remaining"] == 9


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_foreign_pickup_exact_debit(composed, lane):
    home, pool, task = composed
    pool.reopen()  # persisted foreign pickup survives a real SQLite close/reopen
    if lane == "review":
        # Supported ready->review command, synchronized before the real review claim.
        out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert out["results"][0]["status"] == "applied", out
        pool.round()
    pool.enter()
    want = allowance.expected(pool.conn, task)
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    result = claim(pool.conn, task, ttl_seconds=900, claimer="fixture:pool")
    assert result is not None, sync.pop_claim_deferral(task)
    run = pool.conn.execute("SELECT uid FROM task_runs WHERE id=?", (result.current_run_id,)).fetchone()[0]
    response = pool.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run, "lane": lane})
    assert response["lease"]["holder"] == pool.name
    assert response["lease"]["run_uid"] == run
    assert allowance.evidence_ok(want, response["spend_allowance"])
    assert response["reviewer_allowance"]["used"] == 1
    assert response["spend_allowance"]["remaining"] == 9
    # A second run cannot borrow this debit, even through the real response parser.
    other = client.HubLeaseClient(pool.hub).acquire(board_id=BOARD, task_uid=task,
        run_uid=uid(), gateway_id=pool.name, ttl_s=900, lane=lane)
    assert not other.granted and other.spend_allowance is None


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("fault", ["missing", "review", "budget", "charged", "remaining", "basis", "task", "holder", "run"])
def test_real_grant_corrupt_evidence_never_claims(composed, monkeypatch, lane, fault):
    home, pool, task = composed
    if lane == "review":
        out = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert out["results"][0]["status"] == "applied", out
        pool.round()
    pool.enter()
    original = pool.hub.leases
    observed = []

    def corrupt(board, body):
        response = original(board, body)
        observed.append((dict(body), response))
        if body["action"] == "grant" and response["status"] == "applied":
            if fault in {"task", "holder", "run"}:
                field = {"task": "task_id", "holder": "holder", "run": "run_uid"}[fault]
                return {**response, "lease": {**response["lease"], field: "foreign"}}
            evidence = dict(response["spend_allowance"])
            if fault == "missing":
                evidence = None
            else:
                field, value = {"review": ("review_uid", "stale"), "budget": ("budget_event_uid", "stale"),
                    "charged": ("charged", False), "remaining": ("remaining", 9.0),
                    "basis": ("basis", "retired")}[fault]
                evidence[field] = value
            return {**response, "spend_allowance": evidence}
        return response

    monkeypatch.setattr(pool.hub, "leases", corrupt)
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:pool") is None
    expected_reason = "denied" if fault == "run" else allowance.REFUSAL
    assert str(sync.pop_claim_deferral(task)) == expected_reason
    assert kb.get_task(pool.conn, task).current_run_id is None
    # Wrong run identity is rejected by lease admission itself, before an owned
    # grant exists locally. Do not release somebody else's reported run.
    expected_releases = [] if fault == "run" else [True]
    assert [body["unused_grant"] for body, _ in observed if body["action"] == "release"] == expected_releases
    canonical = pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]
    assert canonical["used"] == 1 and canonical["remaining"] == 9  # release never refunds


@pytest.mark.parametrize("kind", ["stale", "removed", "unknown", "malformed"])
def test_local_budget_disagreement_never_burns_private_allowance(composed, kind):
    _, pool, task = composed
    from tests.hermes_cli.test_kanban_spend_allowance import _override
    payload = {"runs": 10, "review_uid": "stale", "runs_basis": allowance.BASIS}
    if kind == "removed":
        payload.pop("runs_basis")
    elif kind == "unknown":
        payload["runs_basis"] = "retired"
    elif kind == "malformed":
        payload["runs"] = True
    # A local forged/stale newest override is not canonical Board authority.
    _override(pool.conn, task, **payload)
    for _ in range(2):
        assert kb.claim_task(pool.conn, task, ttl_seconds=900, claimer="fixture:pool") is None
    canonical = pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]
    assert canonical["used"] == 0 and canonical["remaining"] == 10


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_zero_cumulative_spend_still_exhausts_on_board(composed, lane):
    home, pool, task = composed
    for _ in range(10):
        run = uid()
        reply = pool.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run})
        assert reply["status"] == "applied", reply
        released = pool.hub.leases(BOARD, {"action": "release", "task": task, "run_uid": run, "unused_grant": True})
        assert released["status"] == "applied", released
    if lane == "review":
        result = home.hub.ops(BOARD, [{"client_op_id":uid(), "op":"request_review", "task":task}])
        assert result["results"][0]["status"] == "applied", result
    pool.round()
    pool.reopen()
    from hermes_cli import kanban_spend_budget as budget
    assert budget.spend(pool.conn, task).runs == 0
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:pool") is None
    assert str(sync.pop_claim_deferral(task)) == "reviewer_allowance_exhausted"
    canonical = pool.hub._call("GET", f"/v2/boards/{BOARD}/reviewer-allowance?task={task}")["allowance"]
    assert canonical["used"] == 10 and canonical["remaining"] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_ordinary_after_renewed_scope_gets_canonical_fresh_round(composed, lane, monkeypatch):
    import time
    from hermes_cli import kanban_allowance_maintenance as maintenance
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_reviewer as reviewer
    home, pool, task = composed
    # Real integer-second ordering: ordinary review requires a spending run strictly
    # after the earlier scope release's promotion. This is not a mocked Board clock.
    from tests.waiting import wait_until
    promotion = pool.conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind='promoted_manual'",
                                  (task,)).fetchone()[0]
    assert promotion is not None
    wait_until(lambda: int(time.time()) > promotion, what="real clock past canonical promotion second")
    for _ in range(10):
        run = uid()
        granted = pool.hub.leases(BOARD, {"action": "grant", "task": task, "run_uid": run})
        assert granted["status"] == "applied", granted
        # These isolated fixture runs make no model call. Publish their genuine
        # zero-usage terminal receipt; do not disable the independent accounting gate.
        result = pool.hub.ops(BOARD, [{"client_op_id": uid(), "op": "upsert", "table": "task_events",
            "uid": uid(), "hlc": f"{int(time.time() * 1000):013d}-0000-fixture", "fields": {
                "task_id": task, "run_uid": run, "kind": "worker_result", "created_at": int(time.time()),
                "payload": json.dumps({"usage_final": True, "exit_code": 0, "usage": {
                    "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}})}}])
        assert result["results"][0]["status"] == "applied", result
        released = pool.hub.ops(BOARD, [{"client_op_id": uid(), "op": "release", "task": task,
                                       "run_uid": run, "outcome": "released"}])
        assert released["results"][0]["status"] == "applied", released
    home.round()
    home.enter()
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    maintenance._CURSORS.clear()
    try:
        assert maintenance.sweep(home.conn) == [task]
        stop = decision.pending_episode(home.conn, task)
        assert stop is not None
        episode = stop["uid"]
        kb.add_comment(home.conn, task, "fixture-worker", QUOTE, system=True)
        monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 2))
        with kb.write_txn(home.conn):
            kb._append_event(home.conn, task, reviewer.STARTED, {"episode": episode})
        monkeypatch.setattr(reviewer, "judge", lambda ctx: {
            "decision": "release", "reason": "Finish the integration proof", "remaining_quote": QUOTE})
        assert reviewer.review(home.conn, task, episode) == "settled"
        home.round()
        current = home.hub.reviewer_allowance(BOARD, task)["allowance"]
        assert current["used"] == 0 and current["remaining"] == 10
        if lane == "review":
            result = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
            assert result["results"][0]["status"] == "applied", result
        pool.round()
        pool.enter()
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:ordinary") is not None, sync.pop_claim_deferral(task)
        after = pool.hub.reviewer_allowance(BOARD, task)["allowance"]
        assert after["used"] == 1 and after["remaining"] == 9
    finally:
        maintenance._CURSORS.clear()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_exhausted_real_board_reaches_home_decision_notice(composed, lane, monkeypatch):
    from hermes_cli import kanban_allowance_maintenance as maintenance
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_reviewer as reviewer
    test_zero_cumulative_spend_still_exhausts_on_board(composed, lane)
    home, pool, task = composed
    home.round()
    home.enter()
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    maintenance._CURSORS.clear()
    try:
        assert maintenance.sweep(home.conn) == [task]
        assert kb.get_task(home.conn, task).status == "triage"
        stop = decision.pending_episode(home.conn, task)
        assert stop is not None
        notice = home.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                                   (task, decision.NOTICE)).fetchone()
        assert notice is not None and json.loads(notice[0])["episode"] == stop["uid"]
        home.round()
        pool.round()
        assert pool.hub.reviewer_allowance(BOARD, task)["allowance"]["used"] == 10
        assert kb.get_task(pool.conn, task).status == "triage"
        assert kb.claim_task(pool.conn, task, ttl_seconds=900, claimer="fixture:exhausted") is None
    finally:
        maintenance._CURSORS.clear()



def _cli_release(monkeypatch, home, *args):
    """The real ``hermes kanban release`` command in the home gateway's own isolated HERMES_HOME."""
    import argparse
    from hermes_cli import kanban
    from hermes_cli import kanban_release as release
    from hermes_cli.kanban_parser import build_parser
    home.enter()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home.path))
    monkeypatch.setattr("hermes_cli.kanban_comment_writer.cli_comment_writer",
                        lambda **kw: release.human_writer("isolated-operator"))
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", "release", *args, "--json"]))


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_human_cli_release_supersedes_exhausted_allowance_and_pool_claims(composed, lane, monkeypatch, capsys):
    from hermes_cli import kanban_allowance_maintenance as maintenance
    from hermes_cli import kanban_release as release
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_reviewer as reviewer
    test_zero_cumulative_spend_still_exhausts_on_board(composed, lane)
    home, pool, task = composed
    home.round()
    home.enter()
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    maintenance._CURSORS.clear()
    try:
        assert maintenance.sweep(home.conn) == [task]
        assert decision.pending_episode(home.conn, task) is not None
        # An agent authority cannot replace it: nothing is written and the Board still holds the ten debits.
        decided = "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('scope_renewed','spend_budget_set','stop_released')"
        before = home.conn.execute(decided, (task,)).fetchone()[0]
        agent = ("agent", "20261112_052539_8d8534", "4242:HERMES_SESSION_ID")
        refused = release.release(home.conn, task, writer=agent, env={"HERMES_SESSION_ID": agent[1]},
                                  ancestry=[{"HERMES_SESSION_ID": agent[1]}])
        assert refused["outcome"] == "refused", refused
        assert home.conn.execute(decided, (task,)).fetchone()[0] == before
        assert home.hub.reviewer_allowance(BOARD, task)["allowance"]["remaining"] == 0
        capsys.readouterr()
        assert _cli_release(monkeypatch, home, task, "--runs", "3") == 0
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["outcome"] == "released" and result["allowance_superseded"] is True, result
        assert result["scope_renewed"] is False and result["budget_runs"] == result["spent_runs"] + 3
        assert home.hub.reviewer_allowance(BOARD, task)["allowance"] is None
        home.reopen()
        renewed = [json.loads(r[0]) for r in home.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='scope_renewed' ORDER BY id", (task,))]
        assert renewed[-1]["authority_kind"] == "human" and renewed[-1]["covered_restriction_uids"] == []
        assert renewed[-1]["superseded_allowance"]
        assert allowance.state(home.conn, task) is None
        assert _cli_release(monkeypatch, home, task, "--runs", "3") == 0  # duplicate: nothing more
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["outcome"] == "already_released"
        home.round()
        if lane == "review":
            result = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
            assert result["results"][0]["status"] == "applied", result
        pool.round()
        pool.reopen()
        assert allowance.state(pool.conn, task) is None
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:human") is not None, sync.pop_claim_deferral(task)
        # The home sweep finds nothing stuck or exhausted afterwards: no ready-loop, no second episode.
        home.round()
        home.enter()
        maintenance._CURSORS.clear()
        assert maintenance.sweep(home.conn) == []
    finally:
        maintenance._CURSORS.clear()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_generic_budget_over_active_allowance_is_triaged_not_ready_looped(composed, lane, monkeypatch, capsys):
    """Decided but stuck: a generic budget row cannot reset the private allowance; the Board refuses each claim,
    home maintenance turns that into one decision episode, and only the human release route clears it."""
    from hermes_cli import kanban_allowance_maintenance as maintenance
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_reviewer as reviewer
    home, pool, task = composed
    home.round()
    home.enter()
    # A budget decision is read first (kanban_release_comments): this writer has read every comment here.
    seen = home.conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (task,)).fetchone()[0]
    budget.set_override(home.conn, task, runs=40, cache_read_tokens=0, by="fixture-operator", reason="generic budget",
                        seen=seen)
    home.round()
    assert home.hub.reviewer_allowance(BOARD, task)["allowance"]["state"] == "active"
    if lane == "review":
        result = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert result["results"][0]["status"] == "applied", result
    pool.round()
    pool.reopen()
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:stuck") is None
    assert str(sync.pop_claim_deferral(task)) == "reviewer_allowance_changed"
    assert home.hub.reviewer_allowance(BOARD, task)["allowance"]["used"] == 0  # the refusal burned nothing
    home.round()
    home.enter()
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    maintenance._CURSORS.clear()
    try:
        assert maintenance.sweep(home.conn) == [task]
        stop = decision.pending_episode(home.conn, task)
        assert stop is not None and json.loads(stop["payload"])["allowance"]["superseded"] is False
        notice = home.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                                   (task, decision.NOTICE)).fetchone()
        assert notice is not None and json.loads(notice[0])["episode"] == stop["uid"]
        home.round()
        capsys.readouterr()
        assert _cli_release(monkeypatch, home, task, "--runs", "2") == 0
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["outcome"] == "released" and result["allowance_superseded"] is True, result
        assert home.hub.reviewer_allowance(BOARD, task)["allowance"] is None
        home.round()
        if lane == "review":
            result = home.hub.ops(BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
            assert result["results"][0]["status"] == "applied", result
        pool.round()
        pool.reopen()
        assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:cleared") is not None, sync.pop_claim_deferral(task)
    finally:
        maintenance._CURSORS.clear()
