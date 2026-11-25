from tests.hermes_cli import test_home_probe_custody as probes
from hermes_cli import kanban_ask_resolved as ask, kanban_wake_check as wake
board = probes.board

def test_transferred_hostless_ask_gets_fresh_observation_window(board):
    support = probes.support
    support.prepare_destination(board)
    record = probes.probe_manifest(ask=True)
    support.materialize(board, record)
    support.complete_feed(board, record)
    now = 1000 + wake.ALIVE_EVERY + 240 + wake.SILENT_GRACE + 5
    result = ask.reconcile(board, now=now)
    print('FIRST POST-TRANSFER RECONCILE', result)
    assert result['silent'] == []
