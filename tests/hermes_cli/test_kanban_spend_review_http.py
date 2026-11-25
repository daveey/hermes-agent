"""Real Hub HTTP boundary with a controlled Board-shaped server, not deployed Board."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_spend_reviewer as reviewer
from tests.hermes_cli import test_kanban_spend_review_reconcile as helpers

kanban_home = helpers.kanban_home


def test_actual_http_lost_reply_then_readback(kanban_home, spend_reviewer, monkeypatch):
    requests = []
    receipt = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer test-only"
            if self.path.endswith("/decision-retain/capability"):
                self.respond({"decision_retain": 1, "frozen_replay": 1, "spend_review": 1})
            else:
                query = parse_qs(urlsplit(self.path).query)
                assert query == {"table": ["task_events"], "uid": [receipt["uid"]]}
                self.respond({"board": "test", "table": "task_events", "uid": receipt["uid"],
                              "state": "live", "row": receipt})

        def do_POST(self):
            assert self.path == "/v2/boards/test/ops"
            op = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["ops"][0]
            requests.append(op)
            payload = {k: op[k] for k in ("task", "episode", "verdict", "reason", "max_grants")}
            payload.update(version=1, quote=op["quote"], target=None, runs=10, request_uid=op["event_uid"],
                           request_sha256=hashlib.sha256(json.dumps(op, sort_keys=True, separators=(",", ":"),
                                                                  ensure_ascii=False).encode()).hexdigest(),
                           actor_kind="system", actor_session=None, actor_gateway="home",
                           authority_kind="spend_reviewer", authority_ref="kanban-spend-reviewer")
            receipt.update(uid=op["event_uid"], task_id=op["task"], version=1, kind="spend_review",
                           origin_gateway="home", deleted_at=None, payload=json.dumps(payload))
            # Canonical-shaped commit followed by EOF before the HTTP response.
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ident = SimpleNamespace(gateway="home", key="test-only", url=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(client, "load_identity", lambda: ident)
    monkeypatch.setattr(client, "push", lambda *a, **kw: None)
    monkeypatch.setattr(sync, "sync_state", lambda conn: {"board_id": "test"})
    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "finish", "remaining_quote": helpers.helpers.QUOTE})
    try:
        with kbc.connect_closing() as conn:
            tid, ep = helpers.started(conn)
            assert reviewer.review(conn, tid, ep) == "pending"
        monkeypatch.setattr(reviewer, "judge", lambda ctx: (_ for _ in ()).throw(AssertionError("second judge")))
        with kbc.connect_closing() as conn:
            assert reviewer.review(conn, tid, ep) == "settled"
            assert kb.get_task(conn, tid).status == "triage"
        assert len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
