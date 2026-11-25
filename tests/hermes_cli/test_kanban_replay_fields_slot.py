"""Slot field matrix through real HTTP and both board orders."""
import pytest
from tests.hermes_cli import kanban_replay_field_matrix_support as matrix
live = matrix.live


@pytest.mark.parametrize('field', matrix.FIELDS['slot'][:6])
def test_slot_fields(live, tmp_path, monkeypatch, field):
    matrix.check_field(live, tmp_path, monkeypatch, 'slot', field)
