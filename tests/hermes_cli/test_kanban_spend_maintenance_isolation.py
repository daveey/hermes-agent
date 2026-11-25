"""Malformed stop evidence cannot abort healthy maintenance neighbors."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connect
from tests.hermes_cli import test_kanban_stranded_park as helpers

kanban_home = helpers.kanban_home
own_gateway = helpers.own_gateway


@pytest.mark.parametrize("cause", [1, True, 1.5, [], {}])
@pytest.mark.parametrize("consumer", ["stale", "stranded"])
def test_malformed_stop_isolated_from_stale_neighbor(kanban_home, own_gateway, cause, consumer):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    with connect.connect_closing() as conn:
        bad = helpers._parked(conn, capped=consumer == "stranded")
        good = helpers._parked(conn, capped=consumer == "stranded")
        with kb.write_txn(conn):
            kb._append_event(conn, bad, "spend_budget_triaged", {"cause": cause})
        if consumer == "stale":
            result = sweep_stale_triage(conn, older_than=1, now=int(time.time()) + 100)
            assert good in result
            assert kb.get_task(conn, good).status == "ready"
        else:
            assert helpers._sweep(conn, now=helpers._later(3)) == [good]
            assert kb.get_task(conn, good).human_request
        assert kb.get_task(conn, bad).status == "triage"
        assert not kb.get_task(conn, bad).human_request
