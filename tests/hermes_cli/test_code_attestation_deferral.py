"""Deferred module comparisons move only the time of the check, never its verdict."""
import json

from tests.hermes_cli.test_code_attestation import run_probe

FIXTURE = r'''
        from hermes_cli import code_attestation as ca
        p = pathlib.Path('plugin').resolve(); p.mkdir()
        outside = pathlib.Path('outside').resolve(); outside.mkdir()
        sources = {
            'plain.py': b'def work():\n    return 17\n',
            'data.py': (b'from dataclasses import dataclass\nfrom collections import namedtuple\n'
                        b'@dataclass\nclass Value:\n    number: int = 7\n'
                        b'Pair = namedtuple("Pair", "left right")\n'),
        }
        for name, source in sources.items():
            (p / name).write_bytes(source)
        stray = b'from dataclasses import dataclass\n@dataclass\nclass Stray:\n    number: int = 1\n'
        (outside / 'stray.py').write_bytes(stray)
        a.register_component_root('reviewed', p, 'plugin', digests={
            name: hashlib.sha256(source).hexdigest() for name, source in sources.items()})

        def load(path, source):
            namespace = {'__name__': 'fixture_' + path.stem}
            exec(compile(source, str(path), 'exec'), namespace)
            return namespace

        base = set(a.unknowns())  # what this bare fixture boot latches before any module runs

        def new_unknowns():
            return sorted(set(a.unknowns()) - base)

        def ledger():
            units = sorted((u['component'], u['source_sha256'], u['filename'], u.get('generator') or '')
                           for u in a.executed_units())
            return {'units': units, 'unknowns': new_unknowns()}
'''


def _run(tmp_path, name, body, *, defer):
    target = tmp_path / name
    target.mkdir()
    opt_in = '\n        a.defer_comparisons()\n' if defer else ''
    result = run_probe(target, FIXTURE + opt_in + body)
    # Each arm runs in its own directory; compare ledgers by path below it.
    return json.loads(json.dumps(result).replace(str(target.resolve()), '<run>'))


def test_deferred_ledger_equals_the_inline_ledger(tmp_path):
    body = r'''
        spaces = [load(p / name, source) for name, source in sources.items()]
        spaces.append(load(outside / 'stray.py', stray))
        assert spaces[1]['Value']().number == 7
        owed = len(a._pending)
        early = len(a._units)
        print(json.dumps(dict(ledger(), owed=owed, early=early, left=len(a._pending))))
    '''
    inline = _run(tmp_path, 'inline', body, defer=False)
    deferred = _run(tmp_path, 'deferred', body, defer=True)
    assert inline['owed'] == 0 and inline['early'] > 0
    # Nothing was compared while the modules executed, and nothing is owed after a read.
    assert deferred['owed'] >= 3 and deferred['early'] == 0 and deferred['left'] == 0
    assert deferred['units'] == inline['units']
    assert deferred['unknowns'] == inline['unknowns']
    assert {u[3] for u in deferred['units'] if u[3]} >= {'collections.namedtuple'}
    assert len([u for u in deferred['units'] if 'dataclass' in u[3]]) >= 1
    assert any(x.startswith('generated-origin-unknown:') for x in deferred['unknowns'])
    assert any(x.startswith('unrooted-exec:') for x in deferred['unknowns'])


def test_deferred_comparison_still_refuses_forged_and_changed_code(tmp_path):
    result = _run(tmp_path, 'forged', r'''
        good = sources['plain.py']
        forged = compile(good.replace(b'17', b'18'), str(p / 'plain.py'), 'exec')
        exec(forged, {})
        first = list(a.unknowns())
        exec(compile(sources['data.py'], str(p / 'data.py'), 'exec'), {'__name__': 'fixture_data'})
        (p / 'data.py').write_bytes(sources['data.py'] + b'# changed before the comparison\n')
        print(json.dumps({'first': first, 'second': list(a.unknowns()),
                          'units': len(a.executed_units())}))
    ''', defer=True)
    assert any(x.startswith('executed-code-mismatch:') for x in result['first'])
    # Source that changed between execution and comparison is unknown, never trusted.
    assert any(x.startswith('source-skew:') for x in result['second'])
    assert result['units'] == 0


def test_identity_read_settles_only_the_owning_module(tmp_path):
    result = _run(tmp_path, 'owner', r'''
        plain = load(p / 'plain.py', sources['plain.py'])
        data = load(p / 'data.py', sources['data.py'])
        ok = a.attest_code(plain['work'].__code__)
        units = [u['filename'] for u in a._units]
        generated = a.attest_code(data['Value'].__init__.__code__)
        forged = a.attest_code(plain['work'].__code__.replace(co_name='other'))
        print(json.dumps({'ok': ok, 'units': units, 'generated': generated, 'forged': forged,
                          'unknowns': list(a.unknowns())}))
    ''', defer=True)
    assert result['ok'] is True and result['generated'] is True and result['forged'] is False
    assert [name.rsplit('/', 1)[-1] for name in result['units']] == ['plain.py']
    assert any(x.startswith('unobserved-code:') for x in result['unknowns'])


def test_expired_budget_reports_nothing_and_requests_full_speed(tmp_path):
    result = _run(tmp_path, 'budget', r'''
        ca.SETTLE_DELAY = 3600.0
        for name, source in sources.items():
            load(p / name, source)
        owed = len(a._pending)
        try:
            a.integrity_observation(deadline=time.monotonic() - 1)
            outcome = 'returned'
        except TimeoutError:
            outcome = 'timeout'
        still = len(a._pending)
        limit = time.monotonic() + 10
        while a._pending and time.monotonic() < limit:
            time.sleep(0.01)
        print(json.dumps({'owed': owed, 'outcome': outcome, 'still': still,
                          'left': len(a._pending), 'units': len(a._units)}))
    ''', defer=True)
    assert result['outcome'] == 'timeout'
    assert result['owed'] >= 2 and result['still'] == result['owed']
    # The settler did not wait out its hour: a bounded reader ran out of budget.
    assert result['left'] == 0 and result['units'] >= 2


def test_background_settler_compares_without_any_read(tmp_path):
    result = _run(tmp_path, 'settler', r'''
        ca.SETTLE_DELAY = 0.0
        for name, source in sources.items():
            load(p / name, source)
        limit = time.monotonic() + 10
        while a._pending and time.monotonic() < limit:
            time.sleep(0.01)
        print(json.dumps({'left': len(a._pending), 'units': len(a._units), 'unknown': new_unknowns()}))
    ''', defer=True)
    assert result['left'] == 0 and result['units'] >= 4 and result['unknown'] == []


def test_backlog_is_bounded(tmp_path):
    result = _run(tmp_path, 'bound', r'''
        ca.MAX_PENDING = 4
        ca.SETTLE_DELAY = 3600.0
        peak = 0
        for index in range(12):
            load(p / 'plain.py', sources['plain.py'])
            peak = max(peak, len(a._pending))
        print(json.dumps({'peak': peak, 'units': len(a.executed_units()), 'unknowns': new_unknowns()}))
    ''', defer=True)
    assert result['peak'] <= 4 and result['units'] == 12 and result['unknowns'] == []


def test_interrupted_comparison_is_unknown_not_skipped(tmp_path):
    result = _run(tmp_path, 'interrupt', r'''
        (p / 'plain.py').write_bytes(b'def work():\n    return 18\n')
        load(p / 'plain.py', b'def work():\n    return 18\n')
        observe = a._observe
        def interrupted(*args):
            a._observe = observe
            raise KeyboardInterrupt
        a._observe = interrupted
        try:
            a.unknowns()
            outcome = 'returned'
        except KeyboardInterrupt:
            outcome = 'interrupted'
        print(json.dumps({'outcome': outcome, 'unknowns': new_unknowns(), 'left': len(a._pending)}))
    ''', defer=True)
    assert result['outcome'] == 'interrupted' and result['left'] == 0
    assert 'audit-error:KeyboardInterrupt' in result['unknowns']


def test_root_registered_after_execution_stays_unrooted(tmp_path):
    body = r'''
        late = pathlib.Path('late').resolve(); late.mkdir()
        source = b'VALUE = 5\n'
        (late / 'm.py').write_bytes(source)
        load(late / 'm.py', source)
        a.register_component_root('late', late, 'plugin', digests={'m.py': hashlib.sha256(source).hexdigest()})
        print(json.dumps(ledger()))
    '''
    inline = _run(tmp_path, 'inline', body, defer=False)
    deferred = _run(tmp_path, 'deferred', body, defer=True)
    assert any(x.startswith('unrooted-exec:') and x.endswith('m.py') for x in inline['unknowns'])
    assert deferred == inline


def test_module_executed_later_cannot_vouch_for_an_earlier_generation(tmp_path):
    body = r'''
        import types
        source = (b'def build():\n    from dataclasses import dataclass\n'
                  b'    @dataclass\n    class Late:\n        number: int = 3\n    return Late\n')
        (p / 'plain.py').write_bytes(source)
        a._components['reviewed'][2]['plain.py'] = hashlib.sha256(source).hexdigest()
        code = compile(source, str(p / 'plain.py'), 'exec')
        build = next(c for c in code.co_consts if isinstance(c, types.CodeType))
        types.FunctionType(build, {'__name__': 'fixture_plain', '__builtins__': __builtins__})()
        exec(code, {'__name__': 'fixture_plain'})
        print(json.dumps(ledger()))
    '''
    inline = _run(tmp_path, 'inline', body, defer=False)
    deferred = _run(tmp_path, 'deferred', body, defer=True)
    assert any(x.startswith('generated-origin-unknown:') for x in inline['unknowns'])
    assert deferred == inline


LATE_BUILDER = r'''
        builder = (b'from dataclasses import dataclass\n'
                   b'def build():\n    @dataclass\n    class Late:\n        number: int = 3\n    return Late\n')
'''


def _both(tmp_path, body):
    inline = _run(tmp_path, 'inline', LATE_BUILDER + body, defer=False)
    deferred = _run(tmp_path, 'deferred', LATE_BUILDER + body, defer=True)
    return inline, deferred


def test_reexecution_after_registration_cannot_vouch_for_an_earlier_generation(tmp_path):
    # The first execution is settled alone (unrooted) at registration; the same code
    # object then runs again under the root. The generation belongs to the first.
    inline, deferred = _both(tmp_path, r'''
        late = pathlib.Path('late').resolve(); late.mkdir()
        (late / 'm.py').write_bytes(builder)
        code = compile(builder, str(late / 'm.py'), 'exec')
        ns = {'__name__': 'fixture_m'}
        exec(code, ns)
        ns['build']()
        a.register_component_root('late', late, 'plugin', digests={'m.py': hashlib.sha256(builder).hexdigest()})
        exec(code, ns)
        print(json.dumps(ledger()))
    ''')
    assert any(x.startswith('generated-origin-unknown:') for x in inline['unknowns'])
    assert not [u for u in inline['units'] if u[3]]
    assert deferred == inline


def test_reexecution_after_an_identity_read_cannot_vouch_for_an_earlier_generation(tmp_path):
    # The first execution fails its comparison when an identity read settles it alone.
    inline, deferred = _both(tmp_path, r'''
        a._components['reviewed'][2]['m.py'] = hashlib.sha256(builder).hexdigest()
        (p / 'm.py').write_bytes(builder + b'# drift\n')
        code = compile(builder, str(p / 'm.py'), 'exec')
        ns = {'__name__': 'fixture_m'}
        exec(code, ns)
        ns['build']()
        a.attest_code(ns['build'].__code__)
        (p / 'm.py').write_bytes(builder)
        exec(code, ns)
        print(json.dumps(ledger()))
    ''')
    assert any(x.startswith('generated-origin-unknown:') for x in inline['unknowns'])
    assert not [u for u in inline['units'] if u[3]]
    assert deferred == inline


def test_generation_is_judged_by_the_execution_present_at_the_call(tmp_path):
    # Repeated executions of one code object with generations between them and no
    # read until the end: each generation keeps the verdict it had at its call.
    inline, deferred = _both(tmp_path, r'''
        late = pathlib.Path('late').resolve(); late.mkdir()
        path = late / 'm.py'
        path.write_bytes(builder)
        code = compile(builder, str(path), 'exec')
        ns = {'__name__': 'fixture_m'}
        exec(code, ns)          # first execution: unrooted
        ns['build']()           # unknown origin
        exec(code, ns)          # second execution, still unrooted; the first is compared first
        ns['build']()           # unknown origin
        owed = len(a._pending)
        a.register_component_root('late', late, 'plugin', digests={'m.py': hashlib.sha256(builder).hexdigest()})
        exec(code, ns)          # third execution: clean
        ns['build']()           # attested origin
        exec(code, ns)
        ns['build']()
        print(json.dumps(dict(ledger(), owed=owed)))
    ''')
    assert inline.pop('owed') == 0 and deferred.pop('owed') >= 2
    assert len([x for x in inline['unknowns'] if x.startswith('generated-origin-unknown:')]) == 1
    assert len([u for u in inline['units'] if 'dataclass' in u[3]]) == 2
    assert deferred == inline


def test_later_clean_execution_cannot_vouch_while_everything_is_still_pending(tmp_path):
    # Nothing is settled between the two executions: the skewed first execution is
    # compared against the repaired file, as inline would have seen it skewed.
    inline, deferred = _both(tmp_path, r'''
        a._components['reviewed'][2]['m.py'] = hashlib.sha256(builder).hexdigest()
        path = p / 'm.py'
        code = compile(builder + b'X = 1\n', str(path), 'exec')
        path.write_bytes(builder)
        ns = {'__name__': 'fixture_m'}
        exec(code, ns)          # executed code differs from the reviewed source
        ns['build']()
        clean = compile(builder, str(path), 'exec')
        exec(clean, ns)
        ns['build']()
        print(json.dumps(ledger()))
    ''')
    assert len([x for x in inline['unknowns'] if x.startswith('generated-origin-unknown:')]) == 1
    assert len([u for u in inline['units'] if 'dataclass' in u[3]]) >= 1
    assert deferred == inline


def test_caller_attested_at_the_call_keeps_that_verdict(tmp_path):
    inline, deferred = _both(tmp_path, r'''
        a._components['reviewed'][2]['m.py'] = hashlib.sha256(builder).hexdigest()
        (p / 'm.py').write_bytes(builder)
        code = compile(builder, str(p / 'm.py'), 'exec')
        ns = {'__name__': 'fixture_m'}
        exec(code, ns)
        assert a.attest_code(ns['build'].__code__)
        ns['build']()
        print(json.dumps(ledger()))
    ''')
    assert len([u for u in inline['units'] if 'dataclass' in u[3]]) >= 1
    assert not inline['unknowns']
    assert deferred == inline


def test_exit_with_a_backlog_does_not_hang_on_the_settler(tmp_path):
    result = _run(tmp_path, 'exit', r'''
        ca.SETTLE_DELAY = 0.0
        a._demand.set()
        class Late:
            def __del__(self):
                exec(compile('VALUE = 1', '<late>', 'exec'), {})
        keep = Late()
        for index in range(400):
            load(p / 'plain.py', sources['plain.py'])
        print(json.dumps({'owed': len(a._pending) > 0}))
    ''', defer=True)
    assert result['owed'] is True


def test_only_serve_opts_in_at_bootstrap(monkeypatch, tmp_path):
    import hermes_bootstrap
    from hermes_cli import code_attestation, git_fingerprint, local_work

    calls = []
    monkeypatch.setattr(hermes_bootstrap, '_root', tmp_path)
    monkeypatch.setattr(code_attestation, 'install', lambda *args: None)
    monkeypatch.setattr(code_attestation, 'defer_comparisons', lambda: calls.append('defer'))
    monkeypatch.setattr(code_attestation, 'route_report', lambda: {'route': None})
    monkeypatch.setattr(local_work, 'install', lambda: None)
    monkeypatch.setattr(git_fingerprint, 'process_boot_fingerprint', lambda root: None)
    for argv in (['hermes'], ['hermes', 'gateway', 'run'], ['hermes', 'kanban', 'serve']):
        monkeypatch.setattr(hermes_bootstrap.sys, 'argv', argv)
        hermes_bootstrap._install_code_attestation()
    assert calls == []
    monkeypatch.setattr(hermes_bootstrap.sys, 'argv', ['hermes', 'serve', '--isolated'])
    hermes_bootstrap._install_code_attestation()
    assert calls == ['defer']


def test_uninstalled_opt_in_is_refused(monkeypatch):
    from hermes_cli import code_attestation

    monkeypatch.setattr(code_attestation, '_installed', None)
    assert code_attestation.defer_comparisons() is False
