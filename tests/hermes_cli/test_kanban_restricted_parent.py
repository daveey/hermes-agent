"""Selected-source flow must preserve the Board's restricted-parent gate."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes_flow as flow
from hermes_cli import kanban_lanes_sources as sources
from tests.hermes_cli import test_kanban_lanes_sources as support
from tests.hermes_cli import test_kanban_scope_renewal as renewal_support

board = renewal_support.board
configured = support.configured
conn = support.conn
copies = support.copies


def stopped(db, status, cause):
    support.card(db, 'p', status)
    kb._append_event(db, 'p', 'spend_budget_triaged', {'cause': cause, 'reason': 'fixture stop'})


def child(fleet):
    support.card(fleet, 'c', 'ready')
    fleet.execute("INSERT INTO task_links(parent_id,child_id) VALUES('p','c')")


@pytest.mark.parametrize('status', ['done', 'archived'])
@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
def test_flow_uses_selected_parent_restriction_not_raw_status(copies, configured, status, cause):
    live, fleet = copies
    stopped(live, status, cause)
    support.card(fleet, 'p', status, version=0)
    child(fleet)
    evidence = sources.Sources(live, fleet)
    assert evidence.parents('c') == [('p', f'{status}:restricted')]
    card = flow._card_from_row(fleet, evidence.row('c'), 200, evidence=evidence)
    assert flow.classify_reason(card, flow.classify_step(card)) == 'dep'


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_flow_ordinary_terminal_parent_still_succeeds(copies, configured, status):
    live, fleet = copies
    support.card(live, 'p', status)
    child(fleet)
    assert sources.Sources(live, fleet).parents('c') == []


def test_flow_containment_archive_is_not_success(copies, configured):
    live, fleet = copies
    support.card(live, 'p', 'archived')
    live.execute("UPDATE tasks SET containment_episode='fixture-stop' WHERE id='p'")
    child(fleet)
    assert sources.Sources(live, fleet).parents('c') == [('p', 'archived:restricted')]


def test_supported_scope_renewal_restores_selected_parent_success(board, monkeypatch):
    from hermes_cli.kanban_db_parent_states import mirrored_status
    parent, _, episode = renewal_support.stopped(board, monkeypatch)
    renewal_support.link(board, parent)
    comment = renewal_support.mandate(board, parent)
    assert mirrored_status(board, parent, 'archived') == 'archived:restricted'
    renewal_support.renew(board, parent, episode, comment)
    board.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
    assert mirrored_status(board, parent, 'archived') == 'archived'
