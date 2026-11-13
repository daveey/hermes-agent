"""The quiet partition retains raw evidence and emission-time path custody."""
from tests.hermes_cli.test_code_attestation import run_probe


def test_real_ledger_only_four_emitted_extension_kinds_are_informational(tmp_path):
    result = run_probe(tmp_path, r'''
        import types
        plugin = pathlib.Path('home/plugins/sample.py').resolve()
        plugin.parent.mkdir(parents=True)
        plugin.write_text('value = 1\n')
        a._dependency_exec(compile(plugin.read_bytes(), str(plugin), 'exec'))
        a._module_file('vanished_plugin', str(plugin))
        a._native_load(str(plugin.parent / 'native.so'), sys._getframe())
        a._extension_unknown('generated-origin-unknown:fixture:' + str(plugin), str(plugin))
        blocking, informational = a.quiet_unknown_partition()
        # Later replacement/removal of a module cannot erase its original provenance.
        a._module_file('vanished_plugin', str(root / 'hermes_cli/main.py'))
        assert a.quiet_unknown_partition()[1] == informational
        print(json.dumps(dict(blocking=blocking, informational=informational, raw=a.latched_unknowns())))
    ''')
    assert {r.split(':', 1)[0] for r in result['informational']} == {
        'unrooted-exec', 'unrooted-module', 'generated-origin-unknown', 'native-load'}
    assert set(result['informational']) <= set(result['raw'])
    assert 'bootstrap-provenance-pending' in result['blocking']


def test_core_aliases_and_generic_marks_never_downgrade(tmp_path):
    result = run_probe(tmp_path, r'''
        kinds = ('unrooted-exec:', 'unrooted-module:', 'generated-origin-unknown:', 'native-load:')
        alias = pathlib.Path('core-alias'); alias.symlink_to(root, target_is_directory=True)
        for kind in kinds:
            path = alias / 'hermes_cli/main.py'
            a._extension_unknown(kind + str(path), str(path))
        # A duplicate bare/blocking emission wins even when an earlier event was external.
        a._extension_unknown('unrooted-module:duplicate', '/outside/plugin.py')
        a.mark_unknown('unrooted-module:duplicate')
        remaining = ('unsupported-route:fixture', 'relaunch-exec-mismatch', 'source-skew:fixture',
            'residual:fixture', 'dependency-unreceipted:fixture', 'pth-unreceipted:fixture',
            'prehook-unrooted:fixture', 'non-source-module:fixture', 'fork-inherited-attestation',
            'unattested_bootstrap', 'activity', 'coverage_pending', 'future-kind:fixture')
        for reason in remaining:
            a.mark_unknown(reason)
        a._extension_unknown('unrooted-exec:missing-provenance', None)
        a._extension_unknown('future-kind:extension', '/outside/plugin.py')
        a._extension_unknown('generated-origin-unknown:top', '<top>')
        blocking, extension = a.quiet_unknown_partition()
        print(json.dumps(dict(blocking=blocking, extension=extension, remaining=remaining)))
    ''')
    assert result['extension'] == []
    assert set(result['remaining']) <= set(result['blocking'])
    for kind in ('unrooted-exec:', 'unrooted-module:', 'generated-origin-unknown:', 'native-load:'):
        assert any(r.startswith(kind) and 'core-alias' in r for r in result['blocking'])
    assert 'unrooted-exec:missing-provenance' in result['blocking']
    assert 'future-kind:extension' in result['blocking']
    assert 'generated-origin-unknown:top' in result['blocking']


def test_indexed_native_receipt_failure_is_blocking(tmp_path):
    result = run_probe(tmp_path, r'''
        import base64
        from hermes_cli.code_attestation import _DependencyIndex
        site = pathlib.Path('site-packages').resolve(); site.mkdir()
        native = site / 'native.so'; native.write_bytes(b'not the receipted bytes')
        info = site / 'native-1.0.dist-info'; info.mkdir()
        (info / 'METADATA').write_text('Name: native\nVersion: 1.0\n')
        digest = base64.urlsafe_b64encode(hashlib.sha256(b'original').digest()).decode().rstrip('=')
        (info / 'RECORD').write_text('native.so,sha256=' + digest + ',8\n')
        a._deps = _DependencyIndex(site.parent, [str(site)])
        a._native_load(str(native), sys._getframe())
        print(json.dumps(dict(zip(('blocking', 'extension'), a.quiet_unknown_partition()))))
    ''')
    assert any(r.startswith('dependency-unreceipted:') for r in result['blocking']), result
