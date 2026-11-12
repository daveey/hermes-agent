"""Checkpoint continuations consume completed tools, unlike interrupted new messages."""
from types import SimpleNamespace

import pytest

from gateway.session_update_pause_hook import bind_scope
from tests.gateway.test_turn_update_pause_real_agent import _local_turn
from tui_gateway.update_pause import pause_note


@pytest.mark.parametrize('resuming', [False, True])
def test_checkpoint_resume_does_not_ignore_completed_tool_results(tmp_path, resuming):
    turn = _local_turn()
    turn._runner.session_store = SimpleNamespace(_entries={})
    turn._ctx.history = [{'role': 'tool', 'content': 'verified output'}]
    turn._ctx.message = pause_note('do work')
    with bind_scope(tmp_path, eligible=True, resuming=resuming):
        turn._prepare_turn_message(turn._ctx.history)
    assert ('IGNORE those pending results' in turn._ctx.message) is (not resuming)
    assert pause_note('do work') in turn._ctx.message
