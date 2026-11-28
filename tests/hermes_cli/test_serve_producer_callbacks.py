"""Exact live callback inventory, including identity-bound companions (no plugin discovery)."""
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.serve_producer_protocol import ProtocolError


def registry(tmp_path, monkeypatch):
    path = tmp_path / 'callbacks.py'
    path.write_text('def done(card): return False\n'
                    'def waiting(): return {}\n'
                    'def inputs(): return []\n'
                    'def watch(): return []\n'
                    'def deadline(): return None\n'
                    'def attention_pass(): return None\n'
                    'done.attention_pass = attention_pass\n')
    spec = importlib.util.spec_from_file_location('_producer_callbacks_test', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    activity = SimpleNamespace(_done_checks={'done': module.done}, _waiting_sources={'waiting': module.waiting})
    host = SimpleNamespace(_declared={'waiting': (module.waiting, module.inputs)},
                           _event_sources={}, _done_event_sources={'done': (module.done, module.watch, module.deadline)})
    return module, activity, host


def test_complete_inventory_roundtrips_and_does_not_invoke_callbacks(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture, verify
    module, activity, host = registry(tmp_path, monkeypatch)
    snapshot = capture(activity=activity, host=host)
    assert snapshot['done']['done']['function']['qualname'] == 'done'
    assert snapshot['done']['done']['attention_pass']['qualname'] == 'attention_pass'
    assert snapshot['done']['done']['watch_paths']['qualname'] == 'watch'
    assert snapshot['waiting']['waiting']['inputs']['qualname'] == 'inputs'
    assert snapshot['waiting']['waiting']['watch_paths'] is None
    verify(snapshot, activity=activity, host=host)
    assert module.done({}) is False


@pytest.mark.parametrize('change', ['missing', 'extra', 'replacement', 'inputs', 'watch', 'deadline', 'pass',
                                    'input_identity', 'event_identity', 'orphan_inputs', 'orphan_events', 'orphan_done'])
def test_each_registry_and_identity_bound_companion_is_verified(tmp_path, monkeypatch, change):
    from hermes_cli.serve_producer_callbacks import capture, verify
    module, activity, host = registry(tmp_path, monkeypatch)
    expected = capture(activity=activity, host=host)
    changes = {
        'missing': lambda: activity._done_checks.clear(),
        'extra': lambda: activity._done_checks.update(extra=module.done),
        'replacement': lambda: activity._done_checks.update(done=module.waiting),
        'inputs': lambda: host._declared.update(waiting=(module.waiting, module.watch)),
        'watch': lambda: host._done_event_sources.update(done=(module.done, module.inputs, module.deadline)),
        'deadline': lambda: host._done_event_sources.update(done=(module.done, module.watch, None)),
        'pass': lambda: monkeypatch.setattr(module.done, 'attention_pass', module.inputs),
        'input_identity': lambda: host._declared.update(waiting=(module.done, module.inputs)),
        'event_identity': lambda: host._done_event_sources.update(done=(module.waiting, module.watch, module.deadline)),
        'orphan_inputs': lambda: host._declared.update(orphan=(module.waiting, module.inputs)),
        'orphan_events': lambda: host._event_sources.update(orphan=(module.waiting, module.watch, None)),
        'orphan_done': lambda: host._done_event_sources.update(orphan=(module.done, module.watch, None)),
    }
    changes[change]()
    with pytest.raises(ProtocolError):
        verify(expected, activity=activity, host=host)


def test_loaded_bytes_and_callback_code_are_authenticated(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture, verify
    module, activity, host = registry(tmp_path, monkeypatch)
    expected = capture(activity=activity, host=host)
    original = module.done.__code__
    module.done.__code__ = module.waiting.__code__
    with pytest.raises(ProtocolError):
        verify(expected, activity=activity, host=host)
    module.done.__code__ = original
    path = tmp_path / 'callbacks.py'
    path.write_text(path.read_text() + '\n# changed bytes\n')
    with pytest.raises(ProtocolError):
        verify(expected, activity=activity, host=host)


def test_undeclared_callable_and_disconnected_registries_fail_closed(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture
    _, activity, host = registry(tmp_path, monkeypatch)
    activity._done_checks['bad'] = object()
    with pytest.raises(ProtocolError): capture(activity=activity, host=host)
    with pytest.raises(ProtocolError): capture(activity=activity, host=None)


def test_absent_activity_is_empty_without_importing_or_discovering_plugins(monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture
    monkeypatch.delitem(sys.modules, 'hermes_cli.kanban_session_activity', raising=False)
    monkeypatch.delitem(sys.modules, 'hermes_cli.kanban_annotation_host', raising=False)
    assert capture() == {'done': {}, 'waiting': {}}
    assert 'hermes_cli.kanban_session_activity' not in sys.modules


@pytest.mark.parametrize('change', [
    lambda s: s.clear(),
    lambda s: s.update(done=[]),
    lambda s: s.update(done={'n' + str(i): s['done']['done'] for i in range(65)}),
    lambda s: s['done'].update({'': s['done']['done']}),
    lambda s: s['done'].update({1: s['done']['done']}),
    lambda s: s['done'].update({'n' * 257: s['done']['done']}),
    lambda s: s['done'].update(done=[]),
    lambda s: s['done']['done'].pop('inputs'),
    lambda s: s['done']['done'].update(function=None),
    lambda s: s['done']['done'].update(function=[]),
    lambda s: s['done'].update(done=list(s['done']['done'])),
    lambda s: s['done']['done'].update(function=list(s['done']['done']['function'])),
    lambda s: s['done']['done']['function'].pop('wrappers'),
    lambda s: s['done']['done']['function'].update(sha256='0' * 64),
    lambda s: s['done']['done']['function'].pop('path'),
    lambda s: s['done']['done']['function'].update(wrappers={}),
    lambda s: s['done']['done']['function'].update(wrappers=[{}] * 8),
    lambda s: s['done']['done']['function'].update(wrappers=[{}]),
    lambda s: s['done']['done']['function'].update(wrappers=[None]),
    lambda s: s['done']['done']['function'].update(qualname=''),
    lambda s: s['done']['done']['function'].update(qualname=3),
    lambda s: s['done']['done']['function'].update(sha256=3),
    lambda s: s['done']['done']['function'].update(code='A' * 64),
    lambda s: s['done']['done']['function'].update(defaults='a' * 63),
    lambda s: s['done']['done']['function'].update(path=None),
])
def test_malformed_source_inventory_is_refused(tmp_path, monkeypatch, change):
    from hermes_cli.serve_producer_callbacks import capture, verify_sources
    _, activity, host = registry(tmp_path, monkeypatch)
    expected = capture(activity=activity, host=host)
    change(expected)
    with pytest.raises(ProtocolError): verify_sources(expected)


@pytest.mark.parametrize('which', ['absent', 'large', 'defaults', 'closure', 'wrapper_closure', 'cycle',
                                 'registry_shape', 'registry_size', 'registry_name', 'registry_empty_name',
                                 'registry_nonstring_name', 'companion_list', 'missing_fn', 'companion_shape'])
def test_unreproducible_live_registrations_fail_closed(tmp_path, monkeypatch, which):
    import hermes_cli.serve_producer_callbacks as callbacks
    module, activity, host = registry(tmp_path, monkeypatch)
    value = object()
    closed = lambda: value  # a real captured value, not a missing companion
    def cycle():
        return cycle
    cycle.__dict__['__wrapped__'] = cycle
    changes = {
        'absent': lambda: (tmp_path / 'callbacks.py').unlink(),
        'large': lambda: monkeypatch.setattr(callbacks, 'MAX_SOURCE', 1),
        'defaults': lambda: monkeypatch.setattr(module.done, '__defaults__', (object(),)),
        'closure': lambda: activity._done_checks.update(new=closed),
        'wrapper_closure': lambda: monkeypatch.setattr(module.done, '__wrapped__', module.waiting, raising=False),
        'cycle': lambda: activity._done_checks.update(new=cycle),
        'registry_shape': lambda: monkeypatch.setattr(activity, '_done_checks', []),
        'registry_size': lambda: activity._done_checks.update({str(n): module.done for n in range(64)}),
        'registry_name': lambda: activity._done_checks.update({'x' * 257: module.done}),
        'registry_empty_name': lambda: activity._done_checks.update({'': module.done}),
        'registry_nonstring_name': lambda: activity._done_checks.update({1: module.done}),
        'companion_list': lambda: host._done_event_sources.update(done=[module.done, module.watch, module.deadline]),
        'missing_fn': lambda: activity._done_checks.update(new=None),
        'companion_shape': lambda: host._done_event_sources.update(done=(module.done,)),
    }
    changes[which]()
    with pytest.raises(ProtocolError): callbacks.capture(activity=activity, host=host)


def test_decorated_companion_and_safe_size_limits(tmp_path, monkeypatch):
    import contextlib
    import hermes_cli.serve_producer_callbacks as callbacks
    module, activity, host = registry(tmp_path, monkeypatch)
    monkeypatch.setattr(module.done, 'attention_pass', contextlib.contextmanager(module.attention_pass))
    snapshot = callbacks.capture(activity=activity, host=host)
    assert snapshot['done']['done']['attention_pass']['wrappers']
    callbacks.verify(snapshot, activity=activity, host=host)
    monkeypatch.setattr(callbacks, 'MAX_SOURCE', len((tmp_path / 'callbacks.py').read_bytes()))
    monkeypatch.setattr(module.done, 'attention_pass', None)
    assert callbacks.capture(activity=activity, host=host)['done']['done']['attention_pass'] is None


def test_supported_decorator_depth_is_bounded_without_masking_other_guards(tmp_path, monkeypatch):
    import contextlib
    from hermes_cli.serve_producer_callbacks import MAX_WRAPPERS, capture, verify_sources
    module, activity, host = registry(tmp_path, monkeypatch)
    fn = module.attention_pass
    for _ in range(MAX_WRAPPERS - 1):
        fn = contextlib.contextmanager(fn)
    monkeypatch.setattr(module.done, 'attention_pass', fn)
    valid = capture(activity=activity, host=host)
    verify_sources(valid)
    monkeypatch.setattr(module.done, 'attention_pass', contextlib.contextmanager(fn))
    with pytest.raises(ProtocolError, match='importable function'):
        capture(activity=activity, host=host)


def test_distinct_registries_may_use_the_same_name_with_different_functions(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture, verify_sources
    module, activity, host = registry(tmp_path, monkeypatch)
    activity._waiting_sources['done'] = module.waiting
    host._declared['done'] = (module.waiting, module.inputs)
    expected = capture(activity=activity, host=host)
    assert expected['done']['done']['inputs'] is None
    assert expected['waiting']['done']['inputs']['qualname'] == 'inputs'
    verify_sources(expected)


def test_absent_activity_does_not_hide_orphan_companions():
    from hermes_cli.serve_producer_callbacks import capture
    host = SimpleNamespace(_declared={}, _event_sources={}, _done_event_sources={})
    with pytest.raises(ProtocolError, match='registry shape'):
        capture(activity=None, host=host)


def test_top_level_inventory_type_and_valid_but_oversized_wrapper_list(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_callbacks import capture, verify_sources, MAX_WRAPPERS
    _, activity, host = registry(tmp_path, monkeypatch)
    with pytest.raises(ProtocolError, match='inventory shape'): verify_sources(['done', 'waiting'])
    expected = capture(activity=activity, host=host)
    descriptor = expected['done']['done']['function']
    wrapper = {k: v for k, v in descriptor.items() if k != 'wrappers'}
    descriptor['wrappers'] = [dict(wrapper) for _ in range(MAX_WRAPPERS)]
    with pytest.raises(ProtocolError, match='wrapper inventory'): verify_sources(expected)


def test_source_alias_and_preimport_manifest_digest_checks(tmp_path, monkeypatch):
    import hashlib
    from hermes_cli.serve_producer_callbacks import capture, verify_sources
    from hermes_cli.serve_producer_manifest import verify_declared
    _, activity, host = registry(tmp_path, monkeypatch)
    expected = capture(activity=activity, host=host)
    source = tmp_path / 'callbacks.py'
    alias = tmp_path / 'alias.py'
    alias.symlink_to(source)
    expected['done']['done']['function']['path'] = str(alias)
    with pytest.raises(ProtocolError, match='source digest mismatch'): verify_sources(expected)
    expected = capture(activity=activity, host=host)
    expected['done']['done']['function']['sha256'] = '0' * 64
    manifest = {'version': 1, 'providers': {'p': {'path': source.name,
                'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}},
                'callbacks': {'done': [], 'waiting': []}, 'modes': {}, 'facts': {}, 'callback_registry': expected}
    with pytest.raises(ProtocolError, match='source digest mismatch'): verify_declared(manifest, tmp_path)


def test_cycle_is_rejected_before_repeated_source_io(tmp_path, monkeypatch):
    import hermes_cli.serve_producer_callbacks as callbacks
    _, activity, host = registry(tmp_path, monkeypatch)
    def cycle(): return cycle
    cycle.__dict__['__wrapped__'] = cycle
    activity._done_checks = {'cycle': cycle}
    activity._waiting_sources = {}
    host._declared = host._event_sources = host._done_event_sources = {}
    reads = []
    source = callbacks._source
    def counted(path):
        reads.append(path)
        return source(path)
    monkeypatch.setattr(callbacks, '_source', counted)
    with pytest.raises(ProtocolError): callbacks.capture(activity=activity, host=host)
    assert len(reads) == 1


def test_inventory_replays_in_a_fresh_interpreter(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    from hermes_cli.serve_producer_callbacks import capture
    _, activity, host = registry(tmp_path, monkeypatch)
    expected = capture(activity=activity, host=host)
    probe = tmp_path / 'probe.py'
    probe.write_text('import importlib.util, json, sys\nfrom types import SimpleNamespace\n'
                     'from hermes_cli.serve_producer_callbacks import verify\n'
                     f'spec = importlib.util.spec_from_file_location("_child", {str(tmp_path / "callbacks.py")!r})\n'
                     'm = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m\n'
                     'spec.loader.exec_module(m)\n'
                     'a = SimpleNamespace(_done_checks={"done":m.done}, _waiting_sources={"waiting":m.waiting})\n'
                     'h = SimpleNamespace(_declared={"waiting":(m.waiting,m.inputs)}, _event_sources={}, '
                     '_done_event_sources={"done":(m.done,m.watch,m.deadline)})\n'
                     f'verify(json.loads({json.dumps(expected)!r}), activity=a, host=h)\n'
                     'print("exact callback inventory")\n')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(p) for p in sys.path if p))
    result = subprocess.run([sys.executable, str(probe)], env=env, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'exact callback inventory'


def test_manifest_loaded_boundary_refuses_a_false_empty_registry(tmp_path, monkeypatch):
    import hashlib
    from hermes_cli.serve_producer_manifest import verify_loaded
    _, activity, host = registry(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, 'hermes_cli.kanban_session_activity', activity)
    monkeypatch.setitem(sys.modules, 'hermes_cli.kanban_annotation_host', host)
    path = tmp_path / 'callbacks.py'
    manifest = {'version': 1, 'providers': {'panel': {'path': path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}},
                'callbacks': {'done': [], 'waiting': []}, 'modes': {}, 'facts': {},
                'callback_registry': {'done': {}, 'waiting': {}}}
    with pytest.raises(ProtocolError, match='inventory mismatch'):
        verify_loaded(manifest, tmp_path, modules={})


def test_live_registry_uses_actual_core_registration_seams(tmp_path, monkeypatch):
    from hermes_cli import kanban_annotation_host as host, kanban_session_activity as activity
    from hermes_cli.serve_producer_callbacks import capture, verify
    module, _, _ = registry(tmp_path, monkeypatch)
    for owner, attr in ((activity, '_done_checks'), (activity, '_waiting_sources'),
                        (host, '_declared'), (host, '_event_sources'), (host, '_done_event_sources')):
        monkeypatch.setattr(owner, attr, {})
    # The wake is unrelated to registration identity and must not start a real annotation holder.
    monkeypatch.setattr(host, 'wake_holder', lambda: None)
    activity.register_done_check('done', module.done, watch_paths=module.watch, next_deadline=module.deadline)
    activity.register_waiting_source('waiting', module.waiting, inputs=module.inputs)
    expected = capture()
    assert set(expected['done']) == {'done'} and set(expected['waiting']) == {'waiting'}
    verify(expected)
    activity.register_waiting_source('waiting', module.waiting, inputs=module.watch)
    with pytest.raises(ProtocolError): verify(expected)
