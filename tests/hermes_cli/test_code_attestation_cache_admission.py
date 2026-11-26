"""Cold module checks must not evict reusable generator/interpreter compilations."""
from tests.hermes_cli.test_code_attestation import run_probe
from tests.hermes_cli.test_code_attestation_deps import run as run_dependency_probe


def test_component_observations_do_not_admit_cold_compilations(tmp_path):
    result = run_probe(tmp_path, r'''
        from hermes_cli import code_attestation as ca
        p = pathlib.Path('plugin').resolve(); p.mkdir()
        source = b'def work():\n    return 17\n'
        digests = {f'module{i}.py': hashlib.sha256(source).hexdigest()
                   for i in range(ca.MAX_CACHE_ENTRIES + 2)}
        a.register_component_root('reviewed', p, 'plugin', digests=digests)
        a._compiled(b'pass\n', '<hot>')
        before = list(a._source_cache)
        for name in digests:
            f = p / name; f.write_bytes(source)
            a._observe(compile(source, str(f), 'exec'))
        after = list(a._source_cache)
        print(json.dumps({'before': before, 'after': after,
                          'units': len(a.executed_units())}))
    ''')
    assert result['after'] == result['before']
    assert result['units'] == 34


def test_dependency_observations_do_not_admit_cold_compilations(tmp_path):
    result = run_dependency_probe(tmp_path, r'''
        from hermes_cli import code_attestation as ca
        source = b'VALUE = 7\n'
        files = {f'module{i}.py': source for i in range(ca.MAX_CACHE_ENTRIES + 2)}
        dist('coldpkg', files)
        assert a.register_dependency_environment(SITE)
        a._compiled(b'pass\n', '<hot>')
        before = list(a._source_cache)
        for name in files:
            f = SITE / name
            a._dependency_exec(compile(source, str(f), 'exec'))
        after = list(a._source_cache)
        print(json.dumps({'before': before, 'after': after,
                          'units': sum(pathlib.Path(u['filename']).name in files
                                       for u in a.dependency_receipts()['units'])}))
    ''')
    assert result['after'] == result['before']
    assert result['units'] == 34


def test_uncached_checks_reject_stale_source_and_nested_filename(tmp_path):
    result = run_probe(tmp_path, r'''
        p = pathlib.Path('plugin').resolve(); p.mkdir()
        source = b'def work():\n    return 17\n'
        f = p / 'entry.py'; f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        code = compile(source, str(f), 'exec')
        a._observe(code)
        units = len(a.executed_units())
        child = next(c for c in code.co_consts if isinstance(c, type(code)))
        forged = code.replace(co_consts=tuple(
            child.replace(co_filename='forged.py') if c is child else c
            for c in code.co_consts))
        a._observe(forged)
        stat = f.stat()
        f.write_bytes(source.replace(b'17', b'18'))
        __import__('os').utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        a._observe(code)
        print(json.dumps({'units': len(a.executed_units()), 'before': units,
                          'unknowns': a.unknowns()}))
    ''')
    assert result['units'] == result['before'] == 1
    assert any(x.startswith('executed-code-mismatch:') for x in result['unknowns'])
    assert any(x.startswith('source-skew:') for x in result['unknowns'])
