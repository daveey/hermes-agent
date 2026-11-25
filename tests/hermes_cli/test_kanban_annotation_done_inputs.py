"""Explicit semantic done-check inputs: complete, portable, bound to the registered fn."""
import pytest

from hermes_cli import kanban_annotation_done_inputs as di
from hermes_cli import kanban_session_activity as ksa


def _check(card):
    return False


@pytest.fixture(autouse=True)
def _clean():
    saved = (dict(ksa._done_checks), dict(di._declared))
    ksa._done_checks.clear()
    di._declared.clear()
    yield
    ksa._done_checks.clear()
    ksa._done_checks.update(saved[0])
    di._declared.clear()
    di._declared.update(saved[1])


def test_stable_complete_callback_is_equal_and_changes_with_value():
    box = [1]
    d = ksa.register_done_check('c', _check, inputs=lambda: {'v': box[0]})
    try:
        k1, k2 = di.key(dict(ksa._done_checks)), di.key(dict(ksa._done_checks))
        assert k1 == k2
        box[0] = 2
        assert di.key(dict(ksa._done_checks)) != k1
    finally:
        d()


@pytest.mark.parametrize('value', [{}, [], 0, ''])
def test_empty_values_are_explicit_complete(value):
    d = ksa.register_done_check('c', _check, inputs=lambda: value)
    try:
        assert di.inputs(dict(ksa._done_checks))[1] is True
        assert di.key(dict(ksa._done_checks)) == di.key(dict(ksa._done_checks))
    finally:
        d()


def test_no_checks_is_complete():
    assert di.inputs({}) == ([], True)
    assert di.key({}) == di.key({})


def _boom():
    raise RuntimeError('x')


@pytest.mark.parametrize('cb', [None, lambda: None, _boom, lambda: object(), lambda: {'a': {1, 2}},
                              lambda: {1: 'numeric key'}, lambda: float('nan'), lambda: float('inf'),
                              lambda: {'nested': [float('-inf')]}, 'notcallable'])
def test_missing_none_error_nonportable_is_fresh(cb):
    d = ksa.register_done_check('c', _check, inputs=cb)
    try:
        checks = dict(ksa._done_checks)
        assert di.inputs(checks)[1] is False
        assert di.key(checks) != di.key(checks)
    finally:
        d()


def test_one_undeclared_check_makes_all_fresh():
    d1 = ksa.register_done_check('a', _check, inputs=list)
    d2 = ksa.register_done_check('b', lambda card: True)
    try:
        checks = dict(ksa._done_checks)
        assert di.key(checks) != di.key(checks)
    finally:
        d1()
        d2()


def test_replacement_closure_without_inputs_is_undeclared_and_old_dispose_is_safe():
    d_old = ksa.register_done_check('c', _check, inputs=list)
    new = lambda card: True
    d_new = ksa.register_done_check('c', new)
    checks = dict(ksa._done_checks)
    assert di.key(checks) != di.key(checks)
    d_old()  # stale dispose must not remove the replacement
    assert ksa._done_checks['c'] is new
    d_new()
    assert 'c' not in ksa._done_checks


def test_replacement_with_inputs_keeps_new_declaration_after_old_dispose():
    d_old = ksa.register_done_check('c', _check, inputs=lambda: ['old'])
    new = lambda card: True
    d_new = ksa.register_done_check('c', new, inputs=lambda: ['new'])
    d_old()
    assert di._declared['c'][0] is new
    assert di.key(dict(ksa._done_checks)) == di.key(dict(ksa._done_checks))
    d_new()
    assert 'c' not in di._declared


def test_dispose_removes_declaration():
    d = ksa.register_done_check('c', _check, inputs=list)
    assert 'c' in di._declared
    d()
    assert 'c' not in di._declared and 'c' not in ksa._done_checks


@pytest.mark.parametrize('value', [
    ['stat'], ['stat', 'dep', True], ['stat', 'dep', True, 0],
    {'nested': [('stat', 'dep', True, 0), None, 1.5]},
])
def test_portable_declarations_preserve_all_data_and_copy_mutable_values(value):
    d = ksa.register_done_check('c', _check, inputs=lambda: value)
    try:
        before, complete = di.inputs(dict(ksa._done_checks))
        assert complete
        expected = {'nested': [['stat', 'dep', True, 0], None, 1.5]} if isinstance(value, dict) else value[:]
        assert before[0][2] == expected
        if isinstance(value, list):
            value.append('changed')
        else:
            value['nested'].append('changed')
        assert before[0][2] == expected  # the previous key is a snapshot, not a live alias
        assert di.inputs(dict(ksa._done_checks))[0] != before
    finally:
        d()


def test_closure_identity_distinguishes_same_code():
    def make():
        return lambda card: False
    a, b = make(), make()
    di.declaration('c', a, list)
    assert di.key({'c': a}) == di.key({'c': a})
    old = di.key({'c': a})
    di.declaration('c', b, list)
    assert di.key({'c': b}) != old  # same code and inputs, distinct closure
    assert di.key({'c': b}) == di.key({'c': b})
    assert di.key({'c': a}) != di.key({'c': a})  # replaced fn a is undeclared
