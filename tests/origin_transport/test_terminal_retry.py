"""Compose retry selection with real courier journals and receiving core admission."""
import contextlib
import io
import json

from hermes_cli import kanban_db_connect, kanban_placement as placement, kanban_sync_client
from hermes_cli import kanban_origin_native as native
from hermes_constants import get_hermes_home
from tests.origin_transport import test_forwarding as helpers

network = helpers.network


def test_binding_repair_later_runs_dedupe_at_receiver(network, monkeypatch):
    courier, dbs, service, host, runner, adapter, marker = network

    def send(run):
        with host("worker-7"), monkeypatch.context() as patch:
            patch.setattr(courier, "load_config", lambda: {"gateway": "worker-7", "hub_url": "https://example.com"})
            patch.setattr(courier, "send_queued", lambda db, api: 0)
            patch.setattr(placement, "hub_carries_options", lambda cli: True)
            patch.setattr(placement.reg, "hub_db_path", lambda: get_hermes_home() / "courier/hub.db")
            # Use the exact fixture DB rather than depend on its filename.
            patch.setattr(placement.reg, "_ro", lambda path: __import__("sqlite3").connect(
                dbs["worker-7"].execute("PRAGMA database_list").fetchone()[2]))

            def cli(argv):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = courier.main(argv[1:], api=service)
                return rc, output.getvalue(), ""

            patch.setattr(placement, "hub_runner", cli)
            result = placement.create_remote({"title": "Build", "assignee": "default", "idempotency_key": "one-product"},
                target="hub", self_gw="worker-7", origin_session="chat-one", origin_profile="default",
                origin_gateway="macbook", creator=marker["owner"], run_uid=run, cfg={"creator_session": "worker-session"})
            assert result["ok"], result
            body = json.loads(dbs["worker-7"].execute("SELECT body FROM sent WHERE id=?", (result["request_id"],)).fetchone()[0])
            return {**body, "from": "gw:worker-7"}

    first = send("run-one")
    with host("macbook"):
        ledger = get_hermes_home() / "kanban/origin-ledger.db"
        saved = ledger.read_bytes()
        ledger.unlink()
        with monkeypatch.context() as patch:
            patch.setattr(native, "lookup", lambda *a, **kw: None)
            courier.journal_request(dbs["macbook"], first)
            status, rejection = courier.advance_origin_forward(dbs["macbook"], {}, first, runner, service)
        assert status == "rejected" and rejection["code"] == "origin_forward_binding_missing"
        assert dbs["macbook"].execute("SELECT count(*) FROM sent").fetchone()[0] == 0
        ledger.write_bytes(saved)
    dbs["worker-7"].execute("UPDATE sent SET state='rejected',result=? WHERE id=?", (json.dumps(rejection), first["id"]))
    dbs["worker-7"].commit()
    original = tuple(dbs["worker-7"].execute("SELECT body,state,result FROM sent WHERE id=?", (first["id"],)).fetchone())
    assert send("run-one")["id"] == first["id"]
    second = send("run-two")
    assert second["id"] != first["id"] and send("run-two") == second
    third = send("run-three")
    assert len({first["id"], second["id"], third["id"]}) == 3
    created = []
    for request in (second, third):
        run = request["args"]["run_uid"]
        monkeypatch.setattr(kanban_sync_client.Hub, "snapshot_page", lambda *a, **kw: {
            "table": "leases", "rows": [{"task_id": "t_parent", "holder": "worker-7", "run_uid": run,
                                        "expires": 9999999999999}], "more": False})
        with host("macbook"):
            courier.journal_request(dbs["macbook"], request)
            status, _ = courier.advance_origin_forward(dbs["macbook"], {}, request, runner, service)
            assert status == "pending"
            context = json.loads(dbs["macbook"].execute("SELECT context_json FROM req_ledger WHERE id=?", (request["id"],)).fetchone()[0])
            child = {**context["forward"], "from": "gw:macbook"}
        with host("hub"):
            result = adapter("origin-create", child)
            if not created:
                assert result.get("task_id"), result
                created.append(result["task_id"])
                assert adapter("origin-create", child)["task_id"] == created[0]
            else:
                # A different transport must not adopt another intent's card. The product key
                # refuses a duplicate rather than manufacturing a new binding/second child.
                assert result["status"] == "rejected" and result["code"] == "origin_idempotency_conflict", result
    assert len(created) == 1
    with host("hub"), kanban_db_connect.connect_closing() as board:
        assert board.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert tuple(dbs["worker-7"].execute("SELECT body,state,result FROM sent WHERE id=?", (first["id"],)).fetchone()) == original
