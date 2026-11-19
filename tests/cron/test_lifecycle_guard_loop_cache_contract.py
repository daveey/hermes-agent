"""White-box historical loop-cache contract, not a shell reachability witness."""
import inspect

import pytest

from cron import lifecycle_guard_groups as groups
from cron import lifecycle_guard_substitution_output as output


@pytest.mark.parametrize('prior', ['first.sh', output._OUTPUT + ':first'])
@pytest.mark.parametrize('next_binding', [None, 'second.sh'])
def test_loop_cache_retains_historical_descriptor(monkeypatch, prior, next_binding):
    """A later helper transition must not erase an accumulated may-fact.

    Capture a real walker/cache, then inject the next close transition. This tests
    the helper contract, not reachability across sweeps of one unchanged script.
    """
    captured = []
    original_walk = groups._walk

    def capture(walk, tokens):
        if hasattr(walk, 'restore_groups'):
            captured.append(walk)
        yield from original_walk(walk, tokens)

    monkeypatch.setattr(groups, '_walk', capture)
    assert output._contexts_are_computed(
        'while :; do exec 3<"' + prior + '"; done') is False
    template = captured[-1]
    cache = inspect.getclosurevars(type(template).restore_groups).nonlocals['loop_entries']
    assert cache == {0: {'3': prior}}

    walk = type(template)(template.cat_operands, template.cat_relays_stdin,
                          template.opens_call_arguments, template.peel_reserved_words,
                          template.stdin_redirections)
    group = walk.open_group('done')
    assert group.ordinal == 0
    assert walk.bindings == {'3': prior}
    redirect = '3<&-' if next_binding is None else '3<' + next_binding
    assert output._input_words(['exec', redirect], walk.bindings)[0] == ['exec']
    assert walk.bindings == ({} if next_binding is None else {'3': next_binding})
    assert walk.groups.pop() is group
    walk.restore_groups()

    assert cache == {0: {'3': prior}}, 'historical loop may-fact was erased'
    next_walk = type(template)(template.cat_operands, template.cat_relays_stdin,
                               template.opens_call_arguments, template.peel_reserved_words,
                               template.stdin_redirections)
    next_walk.open_group('done')
    assert next_walk.bindings == {'3': prior}
