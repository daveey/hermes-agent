"""Whole diagnostic inventory identity/retention controls beyond raw metadata."""
from types import ModuleType, SimpleNamespace
import pytest
from hermes_cli import code_attestation as ca
from hermes_cli import code_attestation_inventory as inv


@pytest.mark.parametrize('empty', [False, True])
def test_whole_inventory_without_optional_progress(empty):
    module = ModuleType('ordinary')
    calls = []
    ledger = SimpleNamespace(_modules={}, _generation=0,
        _module_snapshot=lambda: () if empty else (('ordinary', module),),
        _module_file=lambda *args: calls.append(args))
    retained = ledger._modules
    inv.inventory_modules(ledger)
    assert ledger._modules is retained
    assert ledger._generation == (0 if empty else 1)
    assert calls == ([] if empty else [('ordinary', None)])


@pytest.mark.parametrize('change', ['object', 'filename', 'new_identity', 'saturated'])
def test_whole_inventory_retains_old_references_and_accounts_changes(monkeypatch, change):
    module, old = ModuleType('ordinary'), object()
    module.__file__ = 'current.py'
    key = ('ordinary', id(old) if change == 'new_identity' else id(module))
    previous = (old if change in ('object', 'new_identity') else module,
                'previous.py' if change == 'filename' else 'current.py')
    retained = {key: previous}
    calls, unknowns = [], set()
    ledger = SimpleNamespace(_modules=retained, _generation=7,
        _module_snapshot=lambda: (('ordinary', module),),
        _module_file=lambda *args: calls.append(args), mark_unknown=unknowns.add)
    if change == 'saturated':
        monkeypatch.setattr(ca, 'MAX_UNITS', 1)
        ledger._module_snapshot = lambda: (('new', ModuleType('new')),)
    inv.inventory_modules(ledger)
    assert calls == ([('new', None)] if change == 'saturated' else [('ordinary', 'current.py')])
    assert ledger._modules is retained
    if change == 'saturated':
        assert ledger._generation == 7 and retained[key] is previous
        assert unknowns == {'module-inventory-limit'}
    else:
        assert ledger._generation == 8 and unknowns == set()
        assert retained[('ordinary', id(module))] == (module, 'current.py')
        if change == 'new_identity':
            assert retained[key] is previous and len(retained) == 2


def test_file_refusal_preserves_current_identity_and_generation():
    calls = []
    def refuse(*args):
        calls.append(args)
        raise OSError('ordinary_file_failure')
    module = ModuleType('ordinary')
    ledger = SimpleNamespace(_modules={}, _generation=7,
        _module_snapshot=lambda: (('ordinary', module),), _module_file=refuse)
    retained = ledger._modules
    with pytest.raises(OSError, match='^ordinary_file_failure$'):
        inv.inventory_modules(ledger)
    assert calls == [('ordinary', None)]
    assert ledger._modules is retained and not retained and ledger._generation == 7
