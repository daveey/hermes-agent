"""CLI, agent tool and REST retain writer custody after event truncation."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tools import test_kanban_show_queue as helpers

home = helpers.home


def test_completion_custody_on_all_read_surfaces(home, capsys):
    path = home / "report.md"
    path.write_text("surface proof")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="artifact surfaces", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="proof", expected_run_id=claimed.current_run_id,
                                metadata={"artifacts": [str(path)]})
        expected = kb.latest_run(conn, tid).metadata["artifact_receipt"]
        for _ in range(60):
            kb.add_comment(conn, tid, author="default", body="later event")
    assert helpers._tool(tid)["artifact_receipt"] == expected
    assert helpers._rest(tid)["artifact_receipt"] == expected
    rc, text = helpers._cli(capsys, "show", tid, "--json")
    assert rc == 0
    assert json.loads(text)["artifact_receipt"] == expected
    rc, text = helpers._cli(capsys, "show", tid)
    assert rc == 0
    assert expected[0]["machine"] + ":" + str(path) in text
