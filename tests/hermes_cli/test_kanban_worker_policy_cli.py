"""The CLI create entry path enrolls only portable unpinned cards."""
import argparse

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_worker_policy_db as fixtures

conn = fixtures.conn


@pytest.mark.parametrize("extra,expected", [([], "worker-first-v1"), (["--runs-on", "linux"], None)])
def test_cli_pool_create_preserves_pins(conn, monkeypatch, extra, expected):
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": []}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    assert cli._cmd_create(parser.parse_args(["kanban", "create", "Public data analysis", *extra])) == 0
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1
        assert tasks[0].placement_policy == expected
        if extra:
            assert conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tasks[0].id,)).fetchone()[0] == '["linux"]'
