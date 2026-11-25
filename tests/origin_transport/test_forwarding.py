"""Real placement argv -> courier queues -> core adapters -> origin receipt.

Only OS CLI execution, the unrelated placement model response, and service
 delivery/publication are seams. No origin adapter, creation, authorization or
receipt result is fabricated. Supply the other repository explicitly:
scripts/run_tests.sh tests/origin_transport/ -- --courier-client=/path/to/client.py
Stage that exact source outside protected production-home paths first.
"""
import argparse
import contextlib
import copy
import io
import json

import pytest

from hermes_cli import kanban, kanban_db as kb, kanban_placement as placement, profiles
from hermes_cli import kanban_sync_client, kanban_db_connect, kanban_hub_admission
from hermes_cli.kanban_parser import build_parser
from hermes_state import SessionDB
from tools import kanban_tools


class Service:
    """HTTP seam: bind sender to authenticated endpoint, not request arguments."""

    def __init__(self):
        self.requests = {}

    def deliver(self, db, gateway):
        bodies = [json.loads(r[0]) for r in db.execute("SELECT body FROM sent")]
        for body in bodies:
            assert "from" not in body
            envelope = {**body, "from": "gw:" + gateway}
            existing = self.requests.get(body["id"])
            if existing:
                assert all(existing[k] == v for k, v in envelope.items())
            else:
                self.requests[body["id"]] = envelope
        return bodies

    def call(self, method, path, body=None):
        assert method == "GET"
        request = self.requests.get(path.rsplit("/", 1)[-1])
        return (200, {"request": request}) if request else (404, {})

    def finish(self, request, result):
        self.requests[request["id"]] = {**request, "state": "done", "result": result}


@pytest.fixture
def network(request, courier, tmp_path, monkeypatch):
    """``request.param`` False: the owner (hub) board never synced. Default: a synced owner, as every courier
    owner on the fleet is; only there can the origin chat's ``created`` link exist and later renew scope."""
    owner_synced = getattr(request, "param", True)
    roots, dbs = {}, {}
    for gateway in ("macbook", "worker-7", "hub"):
        home = tmp_path / gateway
        home.mkdir()
        (home / "mail.json").write_text(json.dumps({"gateway": gateway}))
        roots[gateway] = home
        with monkeypatch.context() as patch:
            patch.setenv("HERMES_HUB_STATE", str(home / "courier"))
            dbs[gateway] = courier.open_db()
            courier.mset(dbs[gateway], "gateway", gateway)
            dbs[gateway].commit()
    # External placement model response, unrelated to origin authentication.
    monkeypatch.setattr(kanban_hub_admission, "admit", lambda *a, **k: kanban_hub_admission.Verdict(admit=True))
    service = Service()

    @contextlib.contextmanager
    def host(gateway):
        home = roots[gateway]
        with monkeypatch.context() as patch:
            for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_ORIGIN_SESSION"):
                patch.delenv(key, raising=False)
            patch.setenv("HERMES_HOME", str(home))
            patch.setenv("HERMES_KANBAN_HOME", str(home))
            patch.setenv("HERMES_HUB_STATE", str(home / "courier"))
            patch.setenv("HERMES_MAIL_CONFIG", str(home / "mail.json"))
            patch.setenv("HERMES_PROFILE_NAME", "default")
            patch.setattr(profiles, "_get_default_hermes_home", lambda: home)
            patch.setattr(courier, "_home_for", lambda profile: home if profile == "default" else None)
            patch.setattr(kanban_tools, "_profile_has_kanban_toolset", lambda: True)
            yield

    def runner(cfg, argv, stdin):
        assert argv[:3] == ["-p", "default", "kanban"]
        parser = build_parser(argparse.ArgumentParser().add_subparsers())
        args = parser.parse_args(argv[3:])
        output = io.StringIO()
        with monkeypatch.context() as patch, contextlib.redirect_stdout(output):
            patch.setattr("sys.stdin", io.StringIO(stdin.decode()))
            rc = kanban.kanban_command(args)
        return rc, output.getvalue(), ""

    def adapter(action, request):
        rc, out, err = runner({}, ["-p", "default", "kanban", action, "--request-file", "-", "--json"],
                              json.dumps(request).encode())
        assert rc == 0, err
        return json.loads(out)

    # A genuine previously accepted creator binding, not synced ancestry.
    marker = {"binding_id": "binding-parent", "version": 1,
              "owner": {"gateway": "hub", "profile": "default", "board": "default", "task_id": "t_parent"},
              "origin": {"gateway": "macbook", "profile": "default", "session": "chat-one"},
              "task_record_key": "default/default/t_parent"}
    with host("macbook"):
        sessions = SessionDB(db_path=roots["macbook"] / "state.db")
        sessions.create_session("chat-one", "cli")
        sessions.close()
        db = dbs["macbook"]
        record = {**marker["owner"], "id": "t_parent", "status": "running", "origin_bindings": [marker]}
        db.execute("INSERT INTO replica(gw,coll,key,version,data) VALUES(?,?,?,?,?)",
                   ("hub", "tasks", marker["task_record_key"], 1, json.dumps(record)))
        db.commit()
        accepted = adapter("origin-bind", {"id": "bind-parent", "from": "gw:hub", "to_gw": "macbook",
                           "coll": "sessions", "key": "default/chat-one", "action": "origin-bind",
                           "args": {"binding": {**marker, "publication_version": 1}}})
        assert accepted["status"] == "accepted", accepted
    with host("hub"):
        if owner_synced:
            (roots["hub"] / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
        kb.init_db()
        if owner_synced:
            with kanban_db_connect.connect_closing() as board:
                kanban_sync_client.ensure_identity(board, "default", "hub")
    monkeypatch.setattr(kanban_sync_client, "load_identity", lambda: None)
    monkeypatch.setattr(kanban_sync_client.Hub, "machines", lambda self: {
        "me": "macbook", "machines": [{"name": name, "state": "active"} for name in roots]})
    monkeypatch.setattr(kanban_sync_client.Hub, "snapshot_page", lambda *a, **k: {
        "table": "leases", "rows": [{"task_id": "t_parent", "holder": "worker-7", "run_uid": "run-one",
                                    "expires": 9999999999999}], "more": False})
    yield courier, dbs, service, host, runner, adapter, marker
    for db in dbs.values():
        db.close()


def submit(network, monkeypatch):
    courier, _, service, host, _, _, marker = network
    with host("worker-7"), monkeypatch.context() as patch:
        patch.setattr(courier, "load_config", lambda: {"gateway": "worker-7", "hub_url": "https://example.com"})
        patch.setattr(courier, "send_queued", lambda db, api: len(service.deliver(db, "worker-7")))
        patch.setattr(placement, "hub_carries_options", lambda cli: True)
        patch.setattr(placement, "remote_state", lambda rid: "queued")

        def cli(argv):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = courier.main(argv[1:], api=service)
            return rc, output.getvalue(), ""

        patch.setattr(placement, "hub_runner", cli)
        result = placement.create_remote({"title": "Build", "assignee": "default"}, target="hub", self_gw="worker-7",
            origin_session="chat-one", origin_profile="default", origin_gateway="macbook",
            creator=marker["owner"], run_uid="run-one", cfg={"creator_session": "worker-session"})
        assert result["ok"], result
    requests = list(service.requests.values())
    assert len(requests) == 1
    request = requests[0]
    assert request["to_gw"] == "macbook" and request["action"] == "origin-forward"
    assert request["from"] == "gw:worker-7"
    return request


def _owner_create(network, monkeypatch):
    """The origin-create the macbook courier forwards to the hub owner for one submitted placement."""
    courier, dbs, service, host, runner, _, _ = network
    request = submit(network, monkeypatch)
    with host("macbook"):
        courier.journal_request(dbs["macbook"], request)
        courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
        child = service.deliver(dbs["macbook"], "macbook")[0]
    return service.requests[child["id"]]


@pytest.mark.parametrize("network", [False], indirect=True)
def test_a_waiting_owner_create_keeps_the_sender_pending(network, monkeypatch):
    """An owner that cannot create yet only because it never synced answers ``pending`` with the wait's reason:
    the sender's obligation stays open (never ``rejected``), nothing is queued twice and nothing is created."""
    courier, dbs, service, host, runner, _, _ = network
    child = _owner_create(network, monkeypatch)
    request = next(r for r in service.requests.values() if r["action"] == "origin-forward")
    with host("hub"):
        courier.journal_request(dbs["hub"], child)
        status, creation = courier.advance_origin_create(dbs["hub"], {}, child, runner, service)
    assert status == "pending" and creation["code"] == "origin_owner_unsynced", creation
    with host("macbook"):
        sent = dbs["macbook"].execute("SELECT count(*) FROM sent").fetchone()[0]
        for _ in range(3):
            status, result = courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
            assert status == "pending" and result["code"] == "origin_forward_pending", result
        assert dbs["macbook"].execute("SELECT count(*) FROM sent").fetchone()[0] == sent


@pytest.mark.parametrize("network", [False], indirect=True)
def test_forwarding_to_a_never_synced_owner_creates_nothing(network, monkeypatch):
    """Creation authority (kanban_create_authority.require): a never-synced owner cannot hold the origin chat's
    ``created`` link yet, so nothing could renew the card's scope. The create is refused whole, every time,
    and kept: ``pending`` with ``origin_owner_unsynced`` naming the wait (the board may still sync), never the
    reasonless ``origin_dispatch_pending`` and never a terminal ``rejected`` that would drop the create."""
    courier, dbs, service, host, runner, _, _ = network
    child = _owner_create(network, monkeypatch)
    with host("hub"):
        courier.journal_request(dbs["hub"], child)
        for _ in range(2):
            status, creation = courier.advance_origin_create(dbs["hub"], {}, child, runner, service)
            assert status == "pending" and not creation.get("created"), creation
            assert creation["code"] == "origin_owner_unsynced" and creation["refusal"] == "scope_authority_missing"
            assert creation["handoff_complete"] is False
        with kanban_db_connect.connect_closing() as board:
            assert board.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert board.execute("SELECT count(*) FROM kanban_notify_subs").fetchone()[0] == 0


def test_late_binding_rejection_is_durable_on_owning_chat_before_courier_finishes(network, monkeypatch):
    import sqlite3
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_origin_native as native
    courier, dbs, service, host, runner, _, _ = network
    request = submit(network, monkeypatch)
    with host('macbook'):
        home = get_hermes_home()
        (home / 'kanban/origin-ledger.db').unlink()
        monkeypatch.setattr(native, 'lookup', lambda *a, **kw: None)
        courier.journal_request(dbs['macbook'], request)
        for _ in range(2):
            status, result = courier.advance_origin_forward(dbs['macbook'], {}, request, runner, service)
            assert status == 'rejected' and result['code'] == 'origin_forward_binding_missing'
            assert result['handoff_complete'] is False
            with sqlite3.connect(home / 'state.db') as state:
                notices = state.execute("SELECT session_id,content FROM messages WHERE display_kind='kanban_notice'").fetchall()
            assert len(notices) == 1 and notices[0][0] == 'chat-one'
            assert request['id'] in notices[0][1] and 'not created' in notices[0][1]
            assert dbs['macbook'].execute('SELECT count(*) FROM sent').fetchone()[0] == 0
    with host('hub'), kanban_db_connect.connect_closing() as board:
        assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_three_hop_forwarding_creates_once_and_binds_only_macbook(network, monkeypatch):
    courier, dbs, service, host, runner, adapter, _ = network
    request = submit(network, monkeypatch)
    assert submit(network, monkeypatch) == request
    with host("macbook"):
        courier.journal_request(dbs["macbook"], request)
        status, result = courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
        assert status == "pending" and result["handoff_complete"] is False
        child = service.deliver(dbs["macbook"], "macbook")[0]
    child = service.requests[child["id"]]
    assert child["from"] == "gw:macbook" and child["to_gw"] == "hub"
    with host("hub"):
        # Strict receiver must still reject bypassing the origin courier.
        forged = adapter("origin-create", {**child, "from": "gw:worker-7"})
        assert forged["status"] == "rejected", forged
        db = dbs["hub"]
        courier.journal_request(db, child)
        status, creation = courier.advance_origin_create(db, {}, child, runner, service)
        assert status == "pending" and creation.get("created") is True, creation
        assert adapter("origin-create", child)["task_id"] == creation["task_id"]
        with kanban_db_connect.connect_closing() as board:
            assert board.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        marker = creation["origin_creation"]["binding"]
        record = {**marker["owner"], "id": creation["task_id"], "status": "ready", "origin_bindings": [marker]}
        db.execute("INSERT INTO published(coll,key,version,data) VALUES(?,?,?,?)",
                   ("tasks", marker["task_record_key"], 1, json.dumps(record)))
        db.commit()
        courier.freeze_origin_child(db, child)
        bind = service.deliver(db, "hub")[0]
    assert bind["to_gw"] == "macbook"
    assert bind["key"] == "default/chat-one"
    bind = service.requests[bind["id"]]
    with host("macbook"):
        db = dbs["macbook"]
        db.execute("INSERT INTO replica(gw,coll,key,version,data) VALUES(?,?,?,?,?)",
                   ("hub", "tasks", marker["task_record_key"], 1, json.dumps(record)))
        db.commit()
        receipt = adapter("origin-bind", bind)
        assert receipt["status"] == "accepted", receipt
        service.finish(bind, receipt)
    with host("hub"):
        status, completed = courier.advance_origin_create(dbs["hub"], {}, child, runner, service)
        assert status == "done" and completed["handoff_complete"] is True
        service.finish(child, completed)
    with host("macbook"):
        status, completed = courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
        assert status == "done" and completed["origin_receipt"] == receipt
        assert dbs["macbook"].execute("SELECT count(*) FROM sent").fetchone()[0] == 1
    assert all(r["to_gw"] != "worker-7" for r in service.requests.values() if r["action"] == "origin-bind")


@pytest.mark.parametrize("damage", ["sender", "session", "creator", "run"])
def test_untrusted_forward_never_enqueues_owner_create(network, monkeypatch, damage):
    courier, dbs, service, host, runner, _, _ = network
    request = copy.deepcopy(submit(network, monkeypatch))
    if damage == "sender":
        request["from"] = "gw:worker-8"
    elif damage == "session":
        request["args"]["origin"]["session"] = "unknown"
        request["key"] = "default/unknown"
    elif damage == "creator":
        request["args"]["creator"]["task_id"] = "t_unknown"
    else:
        request["args"]["run_uid"] = "run-forged"
    with host("macbook"):
        courier.journal_request(dbs["macbook"], request)
        status, result = courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
        assert status == "rejected", result
        assert dbs["macbook"].execute("SELECT count(*) FROM sent").fetchone()[0] == 0
