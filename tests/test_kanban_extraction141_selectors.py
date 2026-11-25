"""E1/E2 opt-in exact selectors; shared ingress intentionally retains baseline."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import json

import pytest

from agent import model_routing as routing
from agent import kanban_delegation_policy as delegation
from hermes_cli import kanban_model_neutral as neutral

CLASSES = ('class:fast', 'class:standard', 'class:deep', 'class:carbon')
INVALID = ('class:', 'class:bogus', 'class: deep', 'Class:deep', ' class:deep', 'class:deep ', '', None, 0, [], {})


@pytest.mark.parametrize('value', (*CLASSES, *INVALID), ids=repr)
def test_exact_selector_and_delegate_agree(value):
    predicate = getattr(routing, 'exact_class_selector', None)
    assert callable(predicate), 'missing exact class predicate'
    expected = isinstance(value, str) and value in CLASSES
    assert predicate(value) is expected
    assert delegation.is_class(value) is expected


def test_exact_set_equals_allocator_classes():
    assert set(CLASSES) == {f'class:{c}' for c in routing.CLASSES}


def test_delegation_uses_shared_predicate(monkeypatch):
    predicate = getattr(routing, 'exact_class_selector', None)
    assert callable(predicate), 'missing exact class predicate'
    seen = []
    def spy(value):
        seen.append(value)
        return predicate(value)
    # Support either module-qualified or locally imported delegation.
    monkeypatch.setattr(routing, 'exact_class_selector', spy)
    if hasattr(delegation, 'exact_class_selector'):
        monkeypatch.setattr(delegation, 'exact_class_selector', spy)
    assert delegation.is_class('class:deep')
    assert seen == ['class:deep']


@pytest.mark.parametrize('value', ('class:', 'class:bogus', 'class: deep'))
def test_generic_prefix_and_shared_validator_remain_baseline(value):
    assert routing.is_class_selector(value)
    assert neutral.validate_selector(value) == (value, 'auto')


@pytest.mark.parametrize('value', CLASSES)
def test_explicit_card_normalization(value):
    validator = getattr(neutral, 'validate_explicit_selector', None)
    assert callable(validator), 'missing opt-in validator'
    assert validator('  ' + value + '\n', ' auto ') == (value, 'auto')
    assert validator(' ', None) == (None, None)


@pytest.mark.parametrize('model,provider,mandatory', [
    ('class:', None, False), ('class:bogus', None, False), ('class: deep', None, False),
    ('Class:deep', None, False), ('concrete', None, False), ([], None, False),
    ('class:deep', 'anthropic', False), (None, 'auto', False),
    ('class:deep', None, True), ('class:deep', None, 'false'),
])
def test_explicit_selector_refusal(model, provider, mandatory):
    validator = getattr(neutral, 'validate_explicit_selector', None)
    assert callable(validator), 'missing opt-in validator'
    with pytest.raises(ValueError, match='model-neutral') as err:
        validator(model, provider, mandatory=mandatory)
    assert type(err.value) is ValueError


def test_create_wrapper_unknown_class_is_baseline_not_compliance():
    from tools.kanban_tools_model_neutral import model_neutral_create
    seen = []
    @model_neutral_create
    def handler(args):
        seen.append(args)
        return 'baseline handler reached'
    args = {'model': 'class:bogus'}
    assert handler(args) == 'baseline handler reached'
    assert seen == [args]
    result = json.loads(handler({'model': 'concrete'}))
    assert 'model-neutral' in result['error']
    # Concrete selectors are still refused before handler admission; the owner
    # courier now receives an explicit no-write result instead of unknown/pending.
    assert result['code'] == 'kanban_model_restriction'
    assert result['created'] is False and result['handoff_complete'] is False
    assert result['status'] == 'rejected'
    assert seen == [args]
