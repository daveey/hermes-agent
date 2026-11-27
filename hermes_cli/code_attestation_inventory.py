"""Callback-free module metadata for diagnostic capture only, never authorization."""
from types import ModuleType

MAX_MODULE_NAMESPACE_ITEMS = 16384
MAX_INVENTORY_NAMESPACE_ITEMS = 1048576
_MODULE_DICT = ModuleType.__dict__['__dict__']


def inventory_modules(ledger, *, deadline=None, progress=None):
    from hermes_cli import code_attestation as ca
    completed = 0
    if progress is not None:
        progress.set_inventory('module_snapshot', completed)
    retained = _retained_modules(ledger, deadline=deadline)
    budget = [0]
    for name, module in ledger._module_snapshot():
        if progress is not None:
            progress.set_inventory('module_metadata', completed)
        ca._check_inventory_deadline(deadline)
        if type(name) is not str:
            raise ValueError('module_metadata_unavailable')
        filename = _module_filename(module, deadline=deadline, budget=budget)
        if progress is not None:
            progress.set_inventory('module_file', completed)
        ledger._module_file(name, filename)
        key = (name, id(module))
        previous = retained.get(key)
        if previous is not None and previous[0] is module and previous[1] == filename:
            pass
        elif len(ledger._modules) >= ca.MAX_UNITS:
            ledger.mark_unknown('module-inventory-limit')
        else:
            identity = (module, filename)
            ledger._modules[key] = identity
            retained[key] = identity
            ledger._generation += 1
        completed += 1
        if progress is not None:
            progress.set_inventory('module_file', completed)
    if progress is not None:
        progress.set_inventory('finished', completed)


def _retained_modules(ledger, *, deadline):
    from hermes_cli import code_attestation as ca
    retained = ledger._modules
    if type(retained) is not dict or len(retained) > ca.MAX_UNITS:
        raise ValueError('module_metadata_unavailable')
    validated = {}
    try:
        for key, value in dict.items(retained):
            ca._check_inventory_deadline(deadline)
            if type(key) is not tuple or len(key) != 2:
                raise ValueError('module_metadata_unavailable')
            if type(key[0]) is not str or type(key[1]) is not int:
                raise ValueError('module_metadata_unavailable')
            if type(value) is not tuple or len(value) != 2:
                raise ValueError('module_metadata_unavailable')
            if value[1] is not None and type(value[1]) is not str:
                raise ValueError('module_metadata_unavailable')
            validated[key] = value
    except RuntimeError:
        raise ValueError('module_metadata_unavailable') from None
    return validated


def _module_filename(module, *, deadline, budget):
    from hermes_cli import code_attestation as ca
    ca._check_inventory_deadline(deadline)
    if module is None:
        return None
    # Test the real type, not module.__class__ (which can execute callbacks).
    # The base descriptor below bypasses subclass attribute/property overrides.
    if not issubclass(type(module), ModuleType):
        raise ValueError('module_metadata_unavailable')
    try:
        namespace = _MODULE_DICT.__get__(module)
        if type(namespace) is not dict:
            raise ValueError('module_metadata_unavailable')
        filename, dynamic, count = None, False, 0
        for key, value in dict.items(namespace):
            ca._check_inventory_deadline(deadline)
            count += 1
            budget[0] += 1
            if count > MAX_MODULE_NAMESPACE_ITEMS or budget[0] > MAX_INVENTORY_NAMESPACE_ITEMS:
                raise ValueError('module_metadata_unavailable')
            if type(key) is not str:
                raise ValueError('module_metadata_unavailable')
            if key == '__file__':
                filename = value
            elif key == '__getattr__':
                dynamic = True
    except (RuntimeError, TypeError):
        raise ValueError('module_metadata_unavailable') from None
    if filename is not None and type(filename) is not str:
        raise ValueError('module_metadata_unavailable')
    if filename is None:
        if dynamic:
            raise ValueError('module_metadata_unavailable')
        try:
            _check_class_metadata(type(module), deadline=deadline, budget=budget)
        except RuntimeError:
            raise ValueError('module_metadata_unavailable') from None
    # A concrete raw namespace file is authoritative for diagnostics, even when
    # a subclass descriptor overrides ordinary access. Never invoke that access.
    return filename


def _check_class_metadata(cls, *, deadline, budget):
    from hermes_cli import code_attestation as ca
    # Base type descriptors bypass hostile metaclass attribute hooks too.
    mro = type.__dict__['__mro__'].__get__(cls)
    for base in mro:
        ca._check_inventory_deadline(deadline)
        if base is ModuleType:
            return
        namespace = type.__dict__['__dict__'].__get__(base)
        count = 0
        for key in namespace:
            ca._check_inventory_deadline(deadline)
            count += 1
            budget[0] += 1
            if count > MAX_MODULE_NAMESPACE_ITEMS or budget[0] > MAX_INVENTORY_NAMESPACE_ITEMS:
                raise ValueError('module_metadata_unavailable')
            if type(key) is not str or key in ('__file__', '__getattr__', '__getattribute__'):
                raise ValueError('module_metadata_unavailable')
