"""Exercise operator retry/replay through real retained replica scopes, not injected connections."""
import pytest

from hermes_cli import kanban_foreign_card as foreign
from hermes_cli import kanban_operator_call as operator
from hermes_cli import kanban_reader_retention as retention
from tests.hermes_cli import test_kanban_operator_call as cases

fleet = cases.fleet
_REAL_REPLICA = operator._replica


@pytest.mark.parametrize('case', [
    cases.test_worker_comments_on_foreign_card_through_operator_call,
    cases.test_replay_never_applies_twice,
    cases.test_unreachable_board_defers_instead_of_refusing,
    cases.test_home_executes_only_the_card_the_request_names,
    cases.test_result_waits_while_the_board_cannot_vouch_for_it,
    cases.test_rebuilt_replica_is_rescanned_even_at_a_reused_position,
])
def test_real_reader_operator_contract(fleet, monkeypatch, case):
    from pathlib import Path
    path = Path(fleet.rep.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(foreign, 'replica_path', lambda _: path)
    monkeypatch.setattr(operator, '_replica', _REAL_REPLICA)
    try:
        case(fleet)
    finally:
        retention.close_all()
