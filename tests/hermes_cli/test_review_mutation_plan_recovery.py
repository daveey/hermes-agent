"""An unreadable mutation cue must not leak global holder custody. [daveey]"""
import contextvars

import pytest

from hermes_cli import kanban_annotation_counts as counts
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as holders

world = holders.world


def _assert_released():
    available = ksa._annotate_refresh_lock.acquire(blocking=False)
    # This synchronous fixture owns any leaked lock; release even on RED.
    ksa._annotate_refresh_lock.release()
    assert available, 'failed planning leaked the process-wide annotation refresh lock'


def _assert_cleared(scope):
    assert scope not in parts._seen
    assert counts.read_at(scope) is None
    assert counts._use.scope is None
    assert counts._use.keys is None
    assert counts._use.source is None
    assert host._holder_ctx.deadlines is None
    assert getattr(ksa._revalidate.force_read, 'scope', None) is None
    assert getattr(ksa._revalidate.force_read, 'parts', None) is None
    assert not getattr(ksa._revalidate.force_read, 'content_only_boards', False)


def test_corrupt_cue_does_not_permanently_lock_annotation(world):
    holders._refresh()
    cue = host._disk._path(holders.SCOPE).with_suffix('.mutation.json')
    cue.write_text('{broken-json')
    holders._refresh()
    _assert_released()
    _assert_cleared(holders.SCOPE)
    cue.unlink()
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    holders._refresh()
    assert holders._published()[0] == {}


@pytest.mark.parametrize('failed_read', [1, 2], ids=['begin', 'needs_read'])
@pytest.mark.parametrize('error', [OSError, ValueError])
def test_failed_plan_clears_custody_and_healthy_scope_progresses(world, monkeypatch, failed_read, error):
    scope_var = contextvars.ContextVar('review_scope', default=holders.SCOPE)
    monkeypatch.setattr(ksa, '_origin_scope', scope_var.get)
    holders._refresh()
    other = ('annotation-review-healthy', 'default')
    other_context = contextvars.copy_context()
    other_context.run(scope_var.set, other)
    host._known[other] = other_context
    host._disk.touch_demand(other)
    original = host._mutation.current
    calls = 0

    def read(scope):
        nonlocal calls
        if scope == holders.SCOPE:
            calls += 1
            if calls == failed_read:
                raise error('fixture cue unavailable')
        return original(scope)

    monkeypatch.setattr(host._mutation, 'current', read)
    holders._refresh()
    _assert_released()
    _assert_cleared(holders.SCOPE)
    assert other in host._event_ready, 'one failed scope starved its healthy peer'
    assert ksa._last_annotation[other][0] == {'s1': 1}
    monkeypatch.setattr(host._mutation, 'current', original)
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    holders._refresh()
    assert ksa._last_annotation[holders.SCOPE][0] == {}
    assert ksa._last_annotation[other][0] == {}
