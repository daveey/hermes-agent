"""Legacy URL-only discovery retains awaiting fallbacks without a PR reader."""
import json

import pytest

from hermes_cli import kanban_lanes_flow as flow
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
URL = "https://github.com/example/repo/pull/1"


@pytest.mark.parametrize("kind,ref,expected", [
    ("ci", URL, URL),
    ("deploy", URL, URL),
    ("ci", "https://example.com/pull/1", None),
    ("ci", "https://github.com/example/repo/issues/1", None),
    ("ci", {"url": URL}, None),
])
def test_legacy_url_discovery_matches_shared_evidence(conn, configured, kind, ref, expected):
    conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant) "
                 "VALUES ('c','c','scheduled',100,'babybear')")
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                 "VALUES ('c','scheduled',?,100)",
                 (json.dumps({"awaiting": {"kind": kind, "ref": ref}}),))
    row = conn.execute("SELECT * FROM tasks WHERE id='c'").fetchone()
    assert flow._pr_urls(conn, [row]) == [expected]
    assert flow._pr_urls(conn, [row], evidence=flow.Evidence(conn)) == [expected]


def test_legacy_url_discovery_prefers_explicit_pr_over_awaiting(conn, configured):
    conn.execute("INSERT INTO tasks(id,title,status,created_at,tenant,completion_contract) "
                 "VALUES ('c','c','scheduled',100,'babybear',?)", (URL,))
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                 "VALUES ('c','scheduled',?,100)",
                 (json.dumps({"awaiting": {"kind": "ci", "ref": URL + "2"}}),))
    row = conn.execute("SELECT * FROM tasks WHERE id='c'").fetchone()
    assert flow._pr_urls(conn, [row]) == [URL]
    assert flow._pr_urls(conn, [row], evidence=flow.Evidence(conn)) == [URL]
