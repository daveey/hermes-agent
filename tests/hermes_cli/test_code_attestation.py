"""Real subprocess evidence: audit hooks cannot be removed from pytest's process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_probe(tmp_path, body):
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent("""
        import sys, json, hashlib, importlib.util, pathlib, time
        probe_cpu_start = time.process_time()
        probe_wall_start = time.perf_counter()
        sys.path.insert(0, sys.argv[1])
        from hermes_cli.code_attestation import Attestation
        root = pathlib.Path(sys.argv[1])
        a = Attestation(root, subprocess_sha := __import__('subprocess').check_output(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip())
    """) + textwrap.dedent(body))
    result = subprocess.run([sys.executable, str(script), str(ROOT)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False,
                            env={"PATH": os.defpath, "HOME": str(tmp_path),
                                 "TMPDIR": str(tmp_path), "HERMES_HOME": str(tmp_path / "home")},
                            timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_source_bound_exec_records_exact_code_and_reexecution(tmp_path):
    result = run_probe(tmp_path, r"""
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'def work():\n    return 17\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        before = a.generation()
        code = compile(source, str(f), 'exec'); ns = {}; exec(code, ns)
        assert ns['work']() == 17
        assert a.attest_code(ns['work'].__code__)
        assert not a.attest_code(ns['work'].__code__.replace(co_name='other'))
        exec(code, {})
        print(json.dumps({'units': a.executed_units(), 'delta': a.generation()-before,
                          'unknowns': a.unknowns()}))
    """)
    assert len(result["units"]) == 2
    assert result["delta"] >= 2
    assert all(unit["component"] == "reviewed" for unit in result["units"])
    assert any("unobserved-code" in reason for reason in result["unknowns"])


def test_real_stdlib_generators_bound_to_reviewed_origin(tmp_path):
    result = run_probe(tmp_path, r"""
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'import dataclasses, collections\n@dataclasses.dataclass\nclass Point:\n    x: int\nPair = collections.namedtuple("Pair", ["x", "y"])\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        a.install_stdlib_generators()
        ns = {}; exec(compile(source, str(f), 'exec'), ns)
        assert ns['Point'](1).x == 1
        assert ns['Pair'](1, 2).y == 2
        before = a.generation()
        ns['Point'](2); ns['Pair'](3, 4)
        assert a.generation() == before
        print(json.dumps({'units': a.executed_units(), 'unknowns': a.unknowns()}))
    """)
    generators = {u.get('generator') for u in result['units'] if 'generator' in u}
    assert generators == {'dataclasses._FuncBuilder.add_fns_to_class', 'collections.namedtuple'}
    assert not any('generated-' in u or 'unrooted-exec:<string>' in u for u in result['unknowns'])
    assert all(u['component'] == 'reviewed' for u in result['units'])


def test_counterfeit_stdlib_and_frozen_filenames_are_not_exempt(tmp_path):
    result = run_probe(tmp_path, r"""
        import collections
        exec(compile('counterfeit = 1', collections.__file__, 'exec'), {})
        exec(compile('counterfeit = 2', '<frozen os>', 'exec'), {})
        print(json.dumps(a.unknowns()))
    """)
    assert any('stdlib-code-mismatch:' in reason for reason in result)
    assert any('frozen-code-mismatch:' in reason for reason in result)


def test_real_valid_and_stale_pyc(tmp_path):
    result = run_probe(tmp_path, r"""
        import py_compile
        p = pathlib.Path('plugin'); p.mkdir()
        old = b'value = "old"\n'; new = b'value = "new"\n'
        good = (p / 'good.py').resolve(); good.write_bytes(new)
        stale = (p / 'stale.py').resolve(); stale.write_bytes(old)
        for path in (good, stale):
            py_compile.compile(str(path), doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        stale.write_bytes(new)
        a.register_component_root('reviewed', p, 'plugin', digests={
            'good.py': hashlib.sha256(new).hexdigest(),
            'stale.py': hashlib.sha256(new).hexdigest()})
        values = []
        for name, path in (('good', good), ('stale', stale)):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            values.append(mod.value)
        print(json.dumps({'values': values, 'units': a.executed_units(),
                          'unknowns': a.unknowns()}))
    """)
    assert result['values'] == ['new', 'old']
    assert [Path(u['filename']).name for u in result['units']] == ['good.py']
    assert any('executed-code-mismatch:' in r and 'stale.py' in r
               for r in result['unknowns'])


def test_generated_replay_and_plugin_exec_never_laundered(tmp_path):
    result = run_probe(tmp_path, r"""
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'import dataclasses\nclass Meta(type):\n    def __setattr__(cls, name, value):\n        exec("laundered = 1")\n        super().__setattr__(name, value)\n@dataclasses.dataclass\nclass Point(metaclass=Meta):\n    x: int\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin', digests={
            'entry.py': hashlib.sha256(source).hexdigest()})
        a.install_stdlib_generators()
        captured = []
        def capture(event, args):
            if event == 'exec' and args[0].co_filename == '<string>':
                captured.append(args[0])
        sys.addaudithook(capture)
        ns = {}; exec(compile(source, str(f), 'exec'), ns)
        generated = next(c for c in captured if 'Point' not in c.co_names and 'laundered' not in c.co_names)
        before = len(a.executed_units())
        exec(generated, {})
        assert len(a.executed_units()) == before
        print(json.dumps(a.unknowns()))
    """)
    assert 'unrooted-exec:<string>' in result


def test_unregistered_generator_origin_refuses(tmp_path):
    result = run_probe(tmp_path, r"""
        a.install_stdlib_generators()
        import collections
        collections.namedtuple('NotReviewed', ['x'])
        print(json.dumps({'unknowns': a.unknowns(), 'units': a.executed_units()}))
    """)
    assert any(u.startswith('generated-origin-unknown:collections.namedtuple:') for u in result['unknowns']), result
    # The unattested generation is never laundered into an attested executed unit.
    assert not [u for u in result['units'] if u.get('generator') == 'collections.namedtuple'], result


def test_dirty_core_is_compared_to_boot_blob_not_current_disk(tmp_path):
    result = run_probe(tmp_path, r"""
        import subprocess
        p = pathlib.Path('core').resolve(); p.mkdir()
        f = p / 'entry.py'; f.write_text('value = 1\n')
        def git(*args):
            return subprocess.check_output(['git', '-C', str(p), *args], text=True).strip()
        git('init', '-q'); git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'seed')
        sha = git('rev-parse', 'HEAD')
        f.write_text('value = 2\n')
        other = Attestation(p, sha)
        exec(compile(f.read_bytes(), str(f), 'exec'), {})
        print(json.dumps({'unknowns': other.unknowns(), 'units': other.executed_units()}))
    """)
    assert not result['units']
    assert any('source-skew:' in r and r.endswith('entry.py') for r in result['unknowns'])


def test_finite_seed_never_calls_late_import_bootstrap_provenance(tmp_path):
    result = run_probe(tmp_path, r"""
        from hermes_cli.code_attestation import SEED
        print(json.dumps({'seed': a.trusted_bootstrap_seed(), 'expected': SEED,
                          'unknowns': a.unknowns(), 'identity': a.interpreter_identity}))
    """)
    assert 'bootstrap-provenance-pending' in result['unknowns']
    assert 'missing-seed:hermes_bootstrap' in result['unknowns']
    assert set(result['expected']) == {
        'hermes_cli.main', 'hermes_bootstrap', 'hermes_cli', 'hermes_cli._early_recovery',
        'hermes_cli.git_fingerprint', 'hermes_cli.code_attestation', 'hermes_cli.local_work',
        'hermes_cli._launch_template'}
    assert all(r['kind'] == 'trusted_bootstrap_seed' for r in result['seed'])
    assert result['identity']['binary_sha256']


def test_seed_loaded_class_method_skew_is_detected(tmp_path):
    result = run_probe(tmp_path, r"""
        from hermes_cli import local_work
        local_work.Registry.reserve.__code__ = local_work.Registry.reserve.__code__.replace(co_name='skewed')
        other = Attestation(root, subprocess_sha)
        print(json.dumps(other.unknowns()))
    """)
    assert 'seed-code-skew:hermes_cli.local_work' in result


@pytest.mark.platforms("posix")
def test_fork_child_cannot_inherit_clean_attestation(tmp_path):
    result = run_probe(tmp_path, r"""
        import os
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            os.write(write_fd, json.dumps(a.unknowns()).encode())
            os.close(write_fd); os._exit(0)
        os.close(write_fd)
        output = os.read(read_fd, 65536); os.close(read_fd)
        _, status = os.waitpid(pid, 0); assert status == 0
        print(output.decode())
    """)
    assert 'fork-inherited-attestation' in result


def test_third_party_split_compile_exec_stays_unknown(tmp_path):
    """R2 S1: per-template Jinja custody is NOT covered by stdlib receipts."""
    result = run_probe(tmp_path, r"""
        a.install_stdlib_generators()
        import jinja2
        assert jinja2.Template('Hello {{ name }}').render(name='fixture') == 'Hello fixture'
        print(json.dumps(a.unknowns()))
    """)
    assert 'unrooted-exec:<template>' in result


@pytest.mark.parametrize("in_root", [False, True])
def test_real_native_import_latches_after_module_removal(tmp_path, in_root):
    result = run_probe(tmp_path, f"IN_ROOT = {in_root!r}\n" + textwrap.dedent(r'''
        assert '_cffi_backend' not in sys.modules
        spec = importlib.util.find_spec('_cffi_backend')
        assert spec is not None, 'native fixture dependency missing'
        if IN_ROOT:
            a.register_component_root('native', pathlib.Path(spec.origin).parent,
                'plugin', digests={'unused.py': 'unused'})
        else:
            # Outside every indexed site-packages: no installer receipt can apply (R4).
            import shutil
            natdir = pathlib.Path('natdir').resolve(); natdir.mkdir()
            shutil.copy(spec.origin, natdir / pathlib.Path(spec.origin).name)
            sys.path.insert(0, str(natdir))
            importlib.invalidate_caches()
        before = set(a.unknowns())
        import _cffi_backend
        filename = _cffi_backend.__file__
        assert IN_ROOT or filename.startswith(str(pathlib.Path('natdir').resolve())), filename
        assert not a._trusted_stdlib(filename)
        del sys.modules['_cffi_backend']
        del _cffi_backend
        print(json.dumps(sorted(set(a.unknowns()) - before)))
    '''))
    assert 'non-source-module:_cffi_backend' in result


@pytest.mark.parametrize("in_root", [False, True])
def test_real_sourceless_import_refuses(tmp_path, in_root):
    result = run_probe(tmp_path, f"IN_ROOT = {in_root!r}\n" + textwrap.dedent(r'''
        import py_compile
        f = pathlib.Path('sourceless.py').resolve()
        f.write_text('value = 42\n')
        py_compile.compile(str(f), cfile=str(f.with_suffix('.pyc')), doraise=True)
        f.unlink()
        if IN_ROOT:
            a.register_component_root('sourceless', f.parent, 'plugin',
                digests={f.name: hashlib.sha256(b'value = 42\n').hexdigest()})
        sys.path.insert(0, str(f.parent))
        import sourceless
        assert sourceless.value == 42
        print(json.dumps(a.unknowns()))
    '''))
    assert 'non-source-module:sourceless' in result


def test_content_cache_reuses_compile_but_revalidates_bytes(tmp_path):
    result = run_probe(tmp_path, r'''
        import os
        import hermes_cli.code_attestation as ca
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'value = 1\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        code = compile(source, str(f), 'exec')
        original = ca._compile
        calls = []
        def counted(*args, **kwargs):
            calls.append(args[1]); return original(*args, **kwargs)
        ca._compile = counted
        before = a.generation()
        for _ in range(20):
            exec(code, {})
        delta = a.generation() - before
        count = calls.count(str(f))
        stat = f.stat()
        f.write_bytes(b'value = 2\n')
        os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        exec(code, {})
        print(json.dumps({'calls': count, 'delta': delta,
            'units': len(a.executed_units()), 'unknowns': a.unknowns()}))
    ''')
    # The first observation is deliberately not admitted; the second establishes
    # reuse, and the remaining eighteen still share the content-keyed compile.
    assert result['calls'] == 2
    assert result['delta'] == 20
    assert result['units'] == 20
    assert any('source-skew:' in r for r in result['unknowns'])


def test_compiled_cache_is_bounded_and_content_keyed(tmp_path):
    result = run_probe(tmp_path, r'''
        import hermes_cli.code_attestation as ca
        ca.MAX_CACHE_ENTRIES = 3
        ca.MAX_CACHE_BYTES = 4096
        for index in range(40):
            a._compiled(b'value = ' + str(index).encode(), 'fixture.py')
        assert len(a._source_cache) <= 3
        assert a._cache_bytes <= 4096
        code = a._compiled(b'value = 39', 'fixture.py')
        assert code is a._compiled(b'value = 39', 'fixture.py')
        assert code is not a._compiled(b'value = 38', 'fixture.py')
        print(json.dumps(True))
    ''')
    assert result is True


def test_source_reader_bounds_allocation(tmp_path):
    result = run_probe(tmp_path, r'''
        import hermes_cli.code_attestation as ca
        class GuardedStream:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, count):
                assert count == ca.MAX_SOURCE + 1
                return b'x' * count
        class GuardedPath:
            def open(self, mode):
                assert mode == 'rb'
                return GuardedStream()
        try:
            ca._read_bounded(GuardedPath(), ca.MAX_SOURCE)
        except ValueError:
            pass
        else:
            raise AssertionError('read bound not enforced')
        f = pathlib.Path('oversize.py')
        with f.open('wb') as stream:
            stream.truncate(ca.MAX_SOURCE * 4)
        try:
            ca._read_bounded(f, ca.MAX_SOURCE)
        except ValueError as exc:
            assert str(exc) == 'source-limit'
        else:
            raise AssertionError('oversize accepted')
        print(json.dumps(True))
    ''')
    assert result is True


def test_generator_receipts_nest_and_expire_at_next_event(tmp_path):
    result = run_probe(tmp_path, r'''
        import collections
        a.install_stdlib_generators()
        p = pathlib.Path('plugin'); p.mkdir()
        source = b''' + repr("import sys, collections\nlevels = []\ndepth = 0\ndef hook(event, args):\n    global depth\n    if event == 'compile' and args[1] == '<string>' and not getattr(a._tls, 'busy', False) and depth < 3:\n        depth += 1\n        levels.append(len(a._tls.receipts))\n        collections.namedtuple('Nested', ['x'])\n        depth -= 1\nsys.addaudithook(hook)\ncollections.namedtuple('Outer', ['x'])\n") + r'''
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        ns = {'a': a}; exec(compile(source, str(f), 'exec'), ns)
        assert max(ns['levels']) >= 3
        assert len(a._tls.receipts) == 0
        print(json.dumps({'units': a.executed_units(), 'unknowns': a.unknowns()}))
    ''')
    assert len([u for u in result['units'] if u.get('generator') == 'collections.namedtuple']) == 4
    assert not any('generated-' in r for r in result['unknowns'])


def test_receipt_unmatched_count_bytes_overflow_and_pruning(tmp_path):
    result = run_probe(tmp_path, r'''
        import collections
        from types import SimpleNamespace
        import hermes_cli.code_attestation as ca
        a.install_stdlib_generators()
        descriptor = a._generators[id(collections.namedtuple.__code__)]
        site = next(iter(descriptor.sites))
        parent = SimpleNamespace(f_code=(lambda: None).__code__, f_back=None)
        a._remember(parent.f_code, 'fixture')
        frame = SimpleNamespace(f_code=descriptor.code, f_lasti=site, f_back=parent)
        # State-machine fixture: repeated unmatched compiles from one live
        # producer frame, something normal namedtuple does not intentionally do.
        for _ in range(9):
            assert a._generated_event('compile', (b'1', '<string>'), frame)
        assert len(a._tls.receipts) == 8
        assert 'generated-receipt-overflow:collections.namedtuple' in a.unknowns()
        a._tls.receipts = []
        ca.MAX_SOURCE = 4
        for _ in range(3):
            a._generated_event('compile', (b'1234', '<string>'), frame)
        assert len(a._tls.receipts) == 2
        assert sum(len(r[2]) for r in a._tls.receipts) == 8
        other = SimpleNamespace(f_code=descriptor.code, f_lasti=site, f_back=parent)
        a._generated_event('compile', (b'1', '<string>'), other)
        assert len(a._tls.receipts) == 1  # inactive producer references expire
        a._generated_event('exec', (compile('1', '<string>', 'eval'),), other)
        assert not a._tls.receipts
        a._generated_event('exec', (compile('1', '<string>', 'eval'),), other)
        assert 'generated-receipt-missing:collections.namedtuple' in a.unknowns()
        print(json.dumps(True))
    ''')
    assert result is True


def cpu_microfixture(tmp_path):
    """Busy worker diagnostic, NOT a gateway/quiet-target CPU acceptance test.

    Total includes installation, import/exec audit, hashes, compiles, module
    snapshots, generations, receipt bookkeeping, watchdog thread and Git child
    CPU. Nothing is subtracted or excluded as observer overhead.
    """
    return run_probe(tmp_path, r'''
        import resource, collections
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'import collections\nPair = collections.namedtuple("Pair", ["x"])\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        a.install_stdlib_generators()
        code = compile(source, str(f), 'exec')
        setup_cpu = time.process_time() - probe_cpu_start
        phase_start = time.process_time()
        for index in range(300):
            exec(code, {})
            if index % 10 == 0:
                name = 'fixture_import_' + str(index)
                spec = importlib.util.spec_from_file_location(name, f)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            assert a._interpreter_code(collections.namedtuple.__code__)
            if index % 3 == 0:
                a.unknowns()
        phase_cpu = time.process_time() - phase_start
        units = a.executed_units()
        unknowns = a.unknowns()
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        self_cpu = time.process_time() - probe_cpu_start
        print(json.dumps({'setup_self_cpu_s': setup_cpu,
            'hot_phase_all_self_cpu_s': phase_cpu, 'self_cpu_s': self_cpu,
            'child_cpu_s': child.ru_utime + child.ru_stime,
            'total_included_cpu_s': self_cpu + child.ru_utime + child.ru_stime,
            'wall_s': time.perf_counter() - probe_wall_start,
            'execs': 300, 'fresh_imports': 30, 'interpreter_checks': 300, 'snapshots': 101,
            'units': len(units), 'unknowns': unknowns,
            'cache_entries': len(a._source_cache), 'cache_bytes': a._cache_bytes}))
    ''')


@pytest.mark.platforms('posix')
def test_cpu_microfixture_includes_bookkeeping(tmp_path):
    result = cpu_microfixture(tmp_path)
    assert result['units'] == 660
    assert result['total_included_cpu_s'] >= result['hot_phase_all_self_cpu_s'] > 0
    assert not any('generated-' in r for r in result['unknowns'])


def test_interpreter_builtin_import_retains_trusted_distinction(tmp_path):
    result = run_probe(tmp_path, r'''
        before = set(a.unknowns())
        import _csv
        assert (_csv.__spec__.origin == 'built-in' or
                a._trusted_stdlib(_csv.__file__))
        print(json.dumps(sorted(set(a.unknowns()) - before)))
    ''')
    assert not any('non-source-module:_csv' == r for r in result)


def test_snapshot_inventory_is_bounded_and_invalidates_generation(tmp_path):
    result = run_probe(tmp_path, r'''
        from types import ModuleType
        import hermes_cli.code_attestation as ca
        a.unknowns()
        before = a.generation()
        module = ModuleType('snapshot_native')
        module.__file__ = str(pathlib.Path('outside.so').resolve())
        sys.modules['snapshot_native'] = module
        assert 'non-source-module:snapshot_native' in a.unknowns()
        assert a.generation() > before
        stable = a.generation()
        a.unknowns()
        assert a.generation() == stable
        ca.MAX_UNITS = 4
        a.unknowns()
        assert 'module-inventory-limit' in a.unknowns()
        print(json.dumps(True))
    ''')
    assert result is True


def test_generator_events_do_not_disassemble_again(tmp_path):
    result = run_probe(tmp_path, r'''
        import dis
        a.install_stdlib_generators()
        def forbidden(*args, **kwargs):
            raise AssertionError('event-time disassembly')
        dis.get_instructions = forbidden
        p = pathlib.Path('plugin'); p.mkdir()
        source = b'import collections\nPair = collections.namedtuple("Pair", ["x"])\n'
        f = (p / 'entry.py').resolve(); f.write_bytes(source)
        a.register_component_root('reviewed', p, 'plugin',
            digests={'entry.py': hashlib.sha256(source).hexdigest()})
        exec(compile(source, str(f), 'exec'), {})
        print(json.dumps({'units': a.executed_units(), 'unknowns': a.unknowns()}))
    ''')
    assert any(u.get('generator') == 'collections.namedtuple' for u in result['units'])
    assert not any('audit-error' in r or 'generated-' in r for r in result['unknowns'])


def test_boot_tree_pipe_read_is_bounded(tmp_path):
    result = run_probe(tmp_path, r'''
        import hermes_cli.code_attestation as ca
        ca.MAX_TREE = 8
        try:
            ca._boot_blobs(root, subprocess_sha)
        except ValueError as exc:
            assert str(exc) == 'boot-tree-limit'
        else:
            raise AssertionError('tree limit ignored')
        print(json.dumps(True))
    ''')
    assert result is True


def test_extra_prehook_extension_outside_core_refuses(tmp_path):
    result = run_probe(tmp_path, r"""
        f = pathlib.Path('unexpected.py').resolve(); f.write_text('value = 1\n')
        spec = importlib.util.spec_from_file_location('unexpected_extension', f)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['unexpected_extension'] = mod; spec.loader.exec_module(mod)
        other = Attestation(root, subprocess_sha)
        print(json.dumps(other.unknowns()))
    """)
    assert 'prehook-unrooted:unexpected_extension' in result
