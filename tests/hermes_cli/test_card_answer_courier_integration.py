"""Courier + core card-answer integration gate (NOT live proof).

Real: the staged courier's ``handle_requests``/``queue_request``/journals (two separate homes and
databases), core ``kanban_card_answer_runtime.main`` (-> ``source()`` reading actual user-steps
SQL, ``owner()`` applying to the real primary board DB), ``verify`` and ``apply``.

Simulated seams only: (1) an authenticated Hub service/registry (in-memory; NOT TLS, NOT live)
that stamps the sender and serves records; (2) the runtime's OS identity/root (``_profile``) and
credential-bound ``Records`` client. The decision, proof and transaction are never stubbed.

Run: scripts/run_tests.sh tests/hermes_cli/test_card_answer_courier_integration.py \
         -- --card-answer-courier=/path/staged/hermes_hub.py   (stage outside live-home paths first)
Source SQL fixture: protocol-shaped copies of the user-steps ``operations`` table and
``kanban_projections`` DDL. This does not exercise the ActionStore producer (M2).
"""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers

kanban_home = helpers.kanban_home
SRC = dict(gateway="origin", profile="work", session="chat-one")

OPERATIONS_DDL = ("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, namespace TEXT, session TEXT, "
                  "request TEXT, revision INTEGER, idem TEXT, fingerprint TEXT, body TEXT)")  # fixture copy
PROJECTIONS_DDL = ("CREATE TABLE IF NOT EXISTS kanban_projections (namespace TEXT NOT NULL, request_id TEXT NOT NULL, "
                   "session TEXT NOT NULL, board TEXT NOT NULL, task TEXT NOT NULL, snapshot TEXT NOT NULL, "
                   "request TEXT NOT NULL, PRIMARY KEY(namespace,request_id))")  # fixture copy of kanban_mirror.py


class Service:
    """Simulated authenticated Hub registry: stamps sender, routes by to_gw. Not TLS/live."""

    def __init__(self):
        self.requests = {}

    def deliver(self, db, gateway):
        for (raw,) in db.execute("SELECT body FROM sent").fetchall():
            body = json.loads(raw)
            assert "from" not in body
            env = {**body, "from": "gw:" + gateway}
            old = self.requests.get(body["id"])
            if old:
                assert all(old.get(k) == v for k, v in env.items())  # same id, same envelope
            else:
                self.requests[body["id"]] = {**env, "state": "queued"}

    def get(self, rid):
        return copy.deepcopy(self.requests.get(rid))

    def requeue(self, rid):  # server redelivers an already-finished request
        self.requests[rid]["state"] = "queued"


class Api:
    def __init__(self, service, gateway):
        self.service, self.gateway = service, gateway

    def call(self, method, path, body=None):
        s = self.service
        if path.endswith("/claim"):
            mine = [r for r in s.requests.values() if r["to_gw"] == self.gateway and r["state"] == "queued"]
            for r in mine:
                r["state"] = "claimed"
            return 200, {"requests": copy.deepcopy([{k: v for k, v in r.items() if k not in ("state", "result")}
                                                    for r in mine])}
        rid = path.rsplit("/", 1)[-1] if method == "GET" else path.split("/")[-2]
        rec = s.requests.get(rid)
        if method == "GET":
            return (200, {"request": copy.deepcopy(rec)}) if rec else (404, {})
        assert rec and rec["to_gw"] == self.gateway
        if body["status"] in ("done", "rejected"):
            rec["state"], rec["result"] = body["status"], body["result"]
        return 200, {"request": copy.deepcopy(rec)}


class World:
    def __init__(self, courier, tmp_path, owner_home, monkeypatch):
        self.courier, self.mp, self.service = courier, monkeypatch, Service()
        self.homes = {"owner": owner_home, "source": tmp_path / "source-home"}
        self.homes["source"].mkdir()
        self.profile = {"owner": ("default", "owner"), "source": ("work", "origin")}
        self.runs, self.reads, self.dbs = [], [], {}
        for name, home in self.homes.items():
            (home / "mail.json").write_text(json.dumps({"gateway": self.profile[name][1]}))
        self.restart_journal("owner", first=True)
        self.restart_journal("source", first=True)
        assert self.dbs["owner"].execute("PRAGMA database_list").fetchone()[2] != \
            self.dbs["source"].execute("PRAGMA database_list").fetchone()[2]

    @contextlib.contextmanager
    def host(self, name):
        home, (profile, gateway) = self.homes[name], self.profile[name]
        world = self

        class Records:  # authenticated-HTTP seam: reads the simulated registry for THIS gateway
            def __init__(self):
                self.gateway = gateway

            def read(self, rid):
                world.reads.append((name, rid))
                rec = world.service.get(rid)
                if rec is None:
                    raise ca.AnswerRefused("courier_record_missing")
                return rec

        with self.mp.context() as p:
            for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD"):
                p.delenv(k, raising=False)
            p.setenv("HERMES_HOME", str(home))
            p.setenv("HERMES_HUB_STATE", str(home / "courier"))
            p.setenv("HERMES_MAIL_CONFIG", str(home / "mail.json"))
            p.setattr(runtime, "Records", Records)
            p.setattr(runtime, "_profile", lambda: (profile, home))  # OS identity/root seam
            p.setattr(self.courier, "_home_for", lambda prof: home if prof == profile else None)
            yield

    def restart_journal(self, name, first=False):
        with self.host(name):
            if not first:
                self.dbs[name].close()
            self.dbs[name] = self.courier.open_db()
            self.courier.mset(self.dbs[name], "gateway", self.profile[name][1])
            self.dbs[name].commit()

    def runner(self, name):
        def run(cfg, argv, stdin=None, **kw):  # runs the real runtime main in this host
            assert argv[:4] == ["-p", self.profile[name][0], "--run-module", "hermes_cli.kanban_card_answer_runtime"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = runtime.main(argv[4:])
            self.runs.append((name, argv[4], rc, json.loads(out.getvalue())))
            return rc, out.getvalue(), ""
        return run

    def tick(self, name):
        with self.host(name):
            n = self.courier.handle_requests(self.dbs[name], Api(self.service, self.profile[name][1]), {},
                                             self.runner(name))
            self.service.deliver(self.dbs[name], self.profile[name][1])
        return n

    def submit(self, rid, answer, source=SRC):
        with self.host("source"):
            self.courier.queue_request(self.dbs["source"], answer["ownerGateway"], "tasks",
                                       "/".join(answer[k] for k in ("ownerProfile", "board", "task")),
                                       "card-answer", {"answer": answer, "source": source}, rid=rid)
            self.service.deliver(self.dbs["source"], "origin")

    def drive(self, rid, answer, source=SRC):
        """submit -> owner queues proof child -> source journal proves via real SQL -> owner restarts -> receipt."""
        self.submit(rid, answer, source)
        self.tick("owner")
        assert self.service.requests[rid]["state"] == "claimed"  # pending, not finished
        self.tick("source")
        self.restart_journal("owner")  # fresh process/journal handle before the receipt
        self.tick("owner")
        return self.service.requests[rid]


def record_operation(home, answer, source=SRC, op_id=None):
    """Write the user's recorded Desktop operation into the SOURCE home's real-shaped store."""
    path = home / "user-steps/actions.sqlite3"
    path.parent.mkdir(exist_ok=True)
    # Protocol-shaped fixture only; M2 separately exercises the real ActionStore producer.
    ns = json.dumps([source["gateway"], source["profile"]], separators=(",", ":"))
    label = {"plan-a": "Apply plan A", "plan-b": "Apply plan B"}[answer["choice"]]
    cid = "a" if answer["choice"] == "plan-a" else "b"
    req = dict(requestId="request-one", revision=1, choices=[dict(id="a", effect="answer", label="Apply plan A"),
                                                            dict(id="b", effect="answer", label="Apply plan B")])
    op = dict(owner=dict(gatewayId=source["gateway"], profile=source["profile"], storedSessionId=source["session"]),
              operationId=answer["operationId"], recordedAt="2026-01-01T00:00:00+00:00", requestId="request-one",
              revision=1, dispatchState="pending",
              decision=dict(requestId="request-one", expectedRevision=1, choiceId=cid),
              intent=dict(request=req, effect="answer", revisionText=label, executionAuthorized=False,
                          permissionGrants=[]))
    remote = dict(version=1, target={k: v for k, v in answer.items() if k not in ("choice", "operationId")},
                  choiceValues={"a": "plan-a", "b": "plan-b"})
    with sqlite3.connect(path) as c:
        c.execute(OPERATIONS_DDL)
        c.execute(PROJECTIONS_DDL)
        c.execute("INSERT INTO operations(id,namespace,session,request,revision,idem,fingerprint,body) "
                  "VALUES(?,?,?,?,?,?,?,?)", (answer["operationId"], ns, source["session"], "request-one", 1,
                                              answer["operationId"], "fp", json.dumps(op)))
        c.execute("INSERT OR IGNORE INTO kanban_projections VALUES(?,?,?,?,?,?,?)",
                  (ns, "request-one", source["session"], answer["board"], answer["task"],
                   json.dumps(dict(remote=remote)), json.dumps(req)))


@pytest.fixture
def courier(request, tmp_path, monkeypatch):
    source = request.config.getoption("--card-answer-courier")
    if not source:
        pytest.skip("cross-repository test requires --card-answer-courier PATH")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "load-state"))
    spec = importlib.util.spec_from_file_location("card_answer_courier", Path(source).resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def world(courier, kanban_home, tmp_path, monkeypatch):
    with kbc.connect_closing() as conn:  # the REAL primary board DB at <owner root>/kanban.db
        conn.execute("CREATE TABLE kanban_sync_state(gateway_id TEXT, mode TEXT)")
        conn.execute("INSERT INTO kanban_sync_state VALUES ('owner','primary')")
        conn.commit()
    w = World(courier, tmp_path, kanban_home, monkeypatch)
    yield w
    for db in w.dbs.values():
        db.close()


def answer_for(choice="plan-a", op="desktop-op-1"):
    with kbc.connect_closing() as conn:
        a = helpers.setup_answer(conn)
    a.update(choice=choice, operationId=op, ownerGateway="owner")
    return a


def card(answer):
    with kbc.connect_closing() as conn:
        t = kb.get_task(conn, answer["task"])
        return t.status, bool(t.human_request), helpers.comments(conn, answer["task"]), \
            len(kb.list_events(conn, answer["task"]))


@pytest.mark.parametrize("choice,label", [("plan-a", "Apply plan A"), ("plan-b", "Apply plan B")])
def test_end_to_end_receipt_idempotence_and_readback(world, choice, label):
    a = answer_for(choice)
    record_operation(world.homes["source"], a)
    final = world.drive("answer-request-0001", a)
    assert final["state"] == "done", final
    modes = [(n, m, rc) for n, m, rc, _ in world.runs]
    assert modes == [("source", "source", 0), ("owner", "owner", 0)]
    receipt = final["result"]
    assert receipt["operationId"] == a["operationId"] and receipt["choice"] == choice
    assert receipt["originGateway"] == "origin" and receipt["executionStarted"] is False
    status, held, comments, events = card(a)
    assert (status, held) == ("ready", False)
    assert comments == [("user-steps", "Choice recorded in Desktop (remote): "
                         + json.dumps({"label": label, "value": choice}, separators=(",", ":"))
                         + "\nOperation: " + a["operationId"])]
    # proof child was queued through owner 'sent', read via registry by source, journals are separate
    child = next(r for r in world.service.requests.values() if r["action"] == "card-answer-proof")
    assert child["from"] == "gw:owner" and child["to_gw"] == "origin" and child["state"] == "done"
    assert child["result"] == {**child["args"], "gateway": "origin", "recorded": True}
    assert world.dbs["source"].execute("SELECT state FROM req_ledger WHERE id=?", (child["id"],)).fetchone()
    assert world.dbs["owner"].execute("SELECT state FROM req_ledger WHERE id=?", ("answer-request-0001",)).fetchone()
    assert ("source", "answer-request-0001") in world.reads and ("owner", child["id"]) in world.reads

    # repeated delivery of the same request: journal replay, no new effects, same receipt
    world.service.requeue("answer-request-0001")
    runs = len(world.runs)
    world.tick("owner")
    assert world.service.requests["answer-request-0001"]["result"] == receipt
    assert len(world.runs) == runs and card(a) == (status, held, comments, events)

    # a NEW request id for the same operationId: full flow again, receipt replayed, still one effect
    final2 = world.drive("answer-request-0002", a)
    assert final2["state"] == "done" and final2["result"] == receipt
    assert card(a) == (status, held, comments, events)


def test_stale_generation_has_no_effect(world):
    a = answer_for()
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=json_set(human_request,'$.action','New question') WHERE id=?",
                         (a["task"],))
    before = card(a)
    record_operation(world.homes["source"], a)
    final = world.drive("answer-request-stale", a)
    assert final["state"] == "rejected" and final["result"]["refused"] == "stale_ask"
    assert world.runs[-1][:3] == ("owner", "owner", 2)
    assert card(a) == before and before[0] == "blocked"


def test_containment_hold_refuses_without_effect(world):
    a = answer_for()
    with kbc.connect_closing() as conn:
        conn.execute("CREATE TABLE containment_observations(task_id TEXT, episode TEXT)")
        conn.execute("INSERT INTO containment_observations VALUES (?, 'unverified-episode')", (a["task"],))
        conn.commit()
    before = card(a)
    record_operation(world.homes["source"], a)
    final = world.drive("answer-request-held", a)
    assert final["state"] == "rejected" and final["result"]["refused"] == "ask_changed_or_held"
    assert card(a) == before and before[0] == "blocked"


def test_non_ask_human_hold_is_not_released(world):
    a = answer_for()
    with kbc.connect_closing() as conn:  # the hold stops being an exact ask
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=NULL WHERE id=?", (a["task"],))
    before = card(a)
    record_operation(world.homes["source"], a)
    final = world.drive("answer-request-nonask", a)
    assert final["state"] == "rejected" and final["result"]["refused"] == "stale_ask"
    assert card(a) == before and before[0] == "blocked"


def test_unrecorded_source_operation_never_reaches_owner_write(world):
    a = answer_for()
    record_operation(world.homes["source"], answer_for(op="other-op") | {"task": a["task"]})  # other operation only
    before = card(a)
    world.submit("answer-request-noop", a)
    world.tick("owner")
    world.tick("source")
    assert world.runs[-1][:3] == ("source", "source", 2)
    assert world.runs[-1][3]["refused"] == "source_operation_missing"
    world.restart_journal("owner")
    world.tick("owner")
    assert world.service.requests["answer-request-noop"]["state"] == "rejected"
    assert all(m != "owner" for _, m, *_ in world.runs)  # owner core never executed
    assert card(a) == before
