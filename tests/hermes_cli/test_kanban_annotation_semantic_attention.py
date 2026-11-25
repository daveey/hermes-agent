"""Provider replacement is a semantic input, not only a set of check names."""
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as helpers

world = helpers.world


def test_replacing_a_declared_closure_rechecks_attention(world):
    helpers._refresh()
    before = helpers._reads(world)
    assert before[2] > 0
    assert helpers._published()[10] == {'s1': 1}

    def replacement(card):
        world.checks += 1
        return False

    dispose = ksa.register_done_check('fixture', replacement,
                                      watch_paths=lambda: [world.declared], next_deadline=lambda: None)
    try:
        helpers._refresh()
        assert world.checks == before[2] + 1
        assert helpers._published()[10] == {}
        assert helpers._reads(world)[:2] == before[:2]
    finally:
        dispose()
