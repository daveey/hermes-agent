"""Projection work scales with unique cards, not repeated history consumers."""
import json
from collections import Counter

import pytest

from hermes_cli import kanban_lanes_flow as flow
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person
URL = "https://github.com/example/repo/pull/1"


class Published:
    def prefetch(self, urls):
        pass

    def get(self, url):
        return {}


def test_huge_histories_read_and_decode_once(conn, configured, monkeypatch):
    ids = [f"projection-{i}" for i in range(100)]
    # Core connections are autocommit: batch fixture setup, not 40,000 fsyncs.
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO tasks(id,title,status,created_at,tenant) VALUES (?,?,'ready',100,'babybear')",
                     ((tid, tid) for tid in ids))
    # Old publication forces a full backward scan through later harmless churn.
    for tid in ids:
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                     (tid, "commented", json.dumps({"metadata": {"pr": URL}}), 100))
    conn.executemany("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                     ((tid, "heartbeat", '{"projection_noise":true}', 101) for tid in ids for _ in range(400)))
    # Shared parents would otherwise reread their histories for every child.
    conn.executemany("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
                     ((ids[i % 20], tid) for i, tid in enumerate(ids[20:])))
    conn.commit()
    queries = []
    decoded = Counter()
    loads = flow.json.loads

    def count_loads(raw, *args, **kwargs):
        if raw == '{"projection_noise":true}':
            decoded["noise"] += 1
        return loads(raw, *args, **kwargs)

    monkeypatch.setattr(flow.json, "loads", count_loads)
    conn.set_trace_callback(queries.append)
    try:
        result = flow.snapshot(conn, reader=Published(), now=1000)
    finally:
        conn.set_trace_callback(None)
    assert len(result["cards"]) == len(ids)
    assert {c["pr"] for c in result["cards"]} == {URL}
    histories = [q for q in queries if "FROM task_events WHERE task_id=" in q and "ORDER BY id" in q]
    assert len(histories) <= len(ids), len(histories)
    assert decoded["noise"] <= 400 * len(ids), decoded


@pytest.mark.parametrize("status,events", [
    ("ready", [("commented", "not-json"), ("commented", "[]")]),
    ("ready", [("commented", "null"), ("commented", "42"), ("commented", '{"metadata":[]}')]),
    ("ready", [("dispatch_skipped", '{"reason":"quota"}')]),
    ("review", [("commented", json.dumps({"metadata": {"pr": URL, "flow_pr": {
        "url": URL, "merged": True, "deployed": True, "verified": True}}}))]),
    ("scheduled", [("scheduled", json.dumps({"awaiting": {"kind": "ci", "ref": URL}}))]),
    ("scheduled", [("scheduled", json.dumps({"awaiting": {"kind": "deploy", "ref": URL}}))]),
    ("ready", [("review_requested", "{}"), ("review_reopened", "{}")]),
    ("review", [("commented", json.dumps({"metadata": {"pr": URL}})),
                ("commented", json.dumps({"metadata": {"pr": URL + "2"}}))]),
    ("triage", [("created", json.dumps({"creator_task_id": "creator"})),
                ("lane_gated", json.dumps({"reason": "limit:cards_per_day"}))]),
])
def test_shared_evidence_preserves_legacy_card_semantics(conn, configured, status, events):
    conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant,created_by,block_kind) "
                 "VALUES ('c','c',?,100,'babybear','user',?)",
                 (status, flow.lanes.GATE_KIND if status == 'triage' else None))
    for kind, payload in events:
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('c',?,?,100)",
                     (kind, payload))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id='c'").fetchone()
    pol = flow.lanes.safe_policy()
    legacy = flow._card_from_row(conn, row, 1000, Published(), pol)
    evidence = flow.Evidence(conn)
    shared = flow._card_from_row(conn, row, 1000, Published(), pol, evidence=evidence)
    # Decoding is an internal representation change, not a classifier change.
    for card in (legacy, shared):
        card['events'] = [{'kind': e['kind'], 'payload': flow._payload(e)} for e in card['events']]
    assert shared == legacy
    assert flow.classify_step(shared) == flow.classify_step(legacy)
    assert flow.classify_reason(shared, flow.classify_step(shared)) == flow.classify_reason(legacy, flow.classify_step(legacy))
    assert flow.classify_source(shared) == flow.classify_source(legacy)


def test_evidence_shared_between_context_and_snapshot_and_not_between_requests(conn, configured, monkeypatch):
    conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant) VALUES ('c','c','ready',100,'babybear')")
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('c','commented',?,100)",
                 (json.dumps({'metadata': {'pr': URL}}),))
    conn.commit()
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        evidence = flow.Evidence(conn)
        row = evidence.rows[0]
        assert evidence.pr_url(row) == URL
        result = flow.snapshot(conn, reader=Published(), now=1000, evidence=evidence)
    finally:
        conn.set_trace_callback(None)
    assert result['cards'][0]['pr'] == URL
    histories = [q for q in queries if 'FROM task_events WHERE task_id=' in q and 'ORDER BY id' in q]
    assert len(histories) == 1
    conn.execute("UPDATE tasks SET status='archived' WHERE id='c'")
    conn.commit()
    assert flow.snapshot(conn, reader=Published(), now=1000)['cards'] == []


def test_shared_parent_history_and_parent_state_loaded_once(conn, configured, monkeypatch):
    from hermes_cli import kanban_db as kb
    for tid, status in [('parent', 'ready'), ('c1', 'todo'), ('c2', 'todo')]:
        conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant) VALUES (?,?,?,100,'babybear')",
                     (tid, tid, status))
    conn.executemany("INSERT INTO task_links(parent_id,child_id) VALUES ('parent',?)", [('c1',), ('c2',)])
    conn.commit()
    calls = Counter()
    original = kb.unsatisfied_parents

    def counted(db, tid):
        calls[tid] += 1
        return original(db, tid)

    monkeypatch.setattr(kb, 'unsatisfied_parents', counted)
    result = flow.snapshot(conn, reader=Published(), now=1000)
    children = [c for c in result['cards'] if c['id'].startswith('c')]
    assert [c['blockers'] for c in children] == [[{'id': 'parent', 'pr': None}]] * 2
    assert max(calls.values()) == 1


@pytest.mark.parametrize('kind', ['missing', 'triage', 'awaiting', 'contract', 'remote'])
def test_shared_blockers_match_legacy_oracle(conn, configured, kind):
    conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant) VALUES('child','child','todo',100,'babybear')")
    if kind not in ('missing', 'remote'):
        conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant) VALUES('parent','parent',?,100,'babybear')",
                     ('scheduled' if kind == 'awaiting' else 'triage',))
    if kind == 'awaiting':
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('parent','scheduled',?,100)",
                     (json.dumps({'awaiting': {'kind': 'ci', 'ref': URL}}),))
    if kind == 'contract':
        conn.execute("UPDATE tasks SET completion_contract=? WHERE id='parent'", (json.dumps({'pr': URL}),))
    if kind == 'remote':
        conn.execute('CREATE TABLE kanban_sync_remote_parents(parent_id TEXT PRIMARY KEY,status TEXT)')
        conn.execute("INSERT INTO kanban_sync_remote_parents VALUES('parent','ready')")
    conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES('parent','child')")
    evidence = flow.Evidence(conn)
    assert evidence.blockers('child') == flow._blockers(conn, 'child')
    assert bool(evidence.parents('child')) == flow._parents_open(conn, 'child')
