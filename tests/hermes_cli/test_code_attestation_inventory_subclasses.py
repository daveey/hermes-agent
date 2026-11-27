"""Class metadata is opaque; only concrete instance metadata is diagnostic."""
from types import ModuleType, SimpleNamespace

import pytest

from hermes_cli import code_attestation_inventory as inv


def _callback(*args):
    pytest.fail('class metadata callback executed')


def _module(kind, *, mixin_after=False):
    attrs = {
        'attribute': {'__file__': 'class.py'},
        'property': {'__file__': property(_callback)},
        'getattr': {'__getattr__': _callback},
        'getattribute': {'__getattribute__': _callback},
        'slot': {'__slots__': ('__file__',)},
    }
    class OpaqueMeta(type):
        def __getattribute__(self, name):
            pytest.fail('metaclass attribute callback executed')
    parent = OpaqueMeta('Parent', (object,) if mixin_after else (ModuleType,), attrs[kind])
    child = OpaqueMeta('Child', (ModuleType, parent) if mixin_after else (parent,), {})
    return child('opaque')


@pytest.mark.parametrize(('kind', 'mixin_after'), [
    (kind, after) for after in (False, True)
    for kind in ('attribute', 'property', 'getattr', 'getattribute', 'slot')
    if not (after and kind == 'slot')
])
@pytest.mark.parametrize('explicit_none', [False, True])
def test_class_metadata_without_concrete_file_refuses_before_classification(kind, mixin_after, explicit_none):
    module = _module(kind, mixin_after=mixin_after)
    namespace = ModuleType.__dict__['__dict__'].__get__(module)
    if explicit_none:
        namespace['__file__'] = None
    calls = []
    ledger = SimpleNamespace(_modules={}, _generation=7,
        _module_snapshot=lambda: (('opaque', module),),
        _module_file=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inv.inventory_modules(ledger)
    assert calls == [] and ledger._modules == {} and ledger._generation == 7


@pytest.mark.parametrize('kind', ['attribute', 'property', 'getattr', 'getattribute', 'slot'])
def test_concrete_namespace_file_is_authoritative_without_class_callbacks(kind):
    module = _module(kind)
    namespace = ModuleType.__dict__['__dict__'].__get__(module)
    namespace['__file__'] = 'concrete.py'
    assert inv._module_filename(module, deadline=None, budget=[0]) == 'concrete.py'


@pytest.mark.parametrize('mixin_after', [False, True])
def test_benign_subclass_without_file_remains_placeholder(mixin_after):
    class Mixin:
        pass
    bases = (ModuleType, Mixin) if mixin_after else (ModuleType,)
    benign = type('Benign', bases, {})
    assert inv._module_filename(benign('placeholder'), deadline=None, budget=[0]) is None


@pytest.mark.parametrize('limit', ['MAX_MODULE_NAMESPACE_ITEMS', 'MAX_INVENTORY_NAMESPACE_ITEMS'])
def test_class_metadata_scan_is_bounded(monkeypatch, limit):
    class Benign(ModuleType):
        padding = None
    monkeypatch.setattr(inv, limit, 0)
    with pytest.raises(ValueError, match='^module_metadata_unavailable$'):
        inv._check_class_metadata(Benign, deadline=None, budget=[0])


def test_class_metadata_scan_checks_each_item_deadline(monkeypatch):
    from hermes_cli import code_attestation as ca
    class Benign(ModuleType):
        padding = None
    checks = []
    def check(deadline):
        checks.append(deadline)
        if len(checks) == 3:
            raise TimeoutError('class item deadline')
    monkeypatch.setattr(ca, '_check_inventory_deadline', check)
    with pytest.raises(TimeoutError, match='class item deadline'):
        inv._check_class_metadata(Benign, deadline=10, budget=[0])
