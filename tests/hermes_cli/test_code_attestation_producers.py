"""R5 residual producers: typing_inspection (dependency table) and annotationlib.ForwardRef.evaluate.

Each case runs in a fresh subprocess with a real ``Attestation`` audit hook over a fabricated
installer-shaped ``site-packages`` (same harness shape as test_code_attestation_deps.py).
"""
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRELUDE = r'''
import base64, hashlib, importlib, json, os, pathlib, site, subprocess, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.code_attestation import Attestation
import hermes_cli.code_attestation as ca
root = pathlib.Path(sys.argv[1])
a = Attestation(root, subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                              text=True).strip())
SITE = pathlib.Path('env/lib/python3/site-packages').resolve()

def b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()

def dist(name, files):
    SITE.mkdir(parents=True, exist_ok=True)
    lines = []
    for rel, data in files.items():
        path = SITE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        lines.append(f'{rel},sha256={b64(data)},{len(data)}')
    info = SITE / f'{name}-1.0.dist-info'
    info.mkdir()
    (info / 'INSTALLER').write_text('uv\n')
    lines.append(f'{name}-1.0.dist-info/RECORD,,')
    (info / 'RECORD').write_text('\n'.join(lines) + '\n')

def activate():
    a.register_dependency_environment(SITE)
    site.addsitedir(str(SITE))
    sys.path.insert(0, str(SITE))  # the fabricated dists shadow any real install of the same name

def new():
    return sorted(set(a.unknowns()) - BASE)

def generated():
    return [u for u in a.dependency_receipts()['units'] if u['kind'] == 'generated']

# A fake typing_inspection whose two producers mirror the real ones (exec of a built string).
TYPING_OBJECTS = b"""
import typing

def _compile_identity_check_function(member, function_name):
    globals_ = {'_obj': getattr(typing, member)}
    func_code = f"def {function_name}(obj, /):\\n    return obj is _obj\\n"
    locals_ = {}
    exec(func_code, globals_, locals_)
    return locals_[function_name]

def _compile_isinstance_check_function(member, function_name):
    globals_ = {'_obj': getattr(typing, member)}
    func_code = f"def {function_name}(obj, /):\\n    return isinstance(obj, _obj)\\n"
    locals_ = {}
    exec(func_code, globals_, locals_)
    return locals_[function_name]

is_any = _compile_identity_check_function('Any', 'is_any')
is_generic = _compile_isinstance_check_function('Generic', 'is_generic')
"""
PKG = {'typing_inspection/__init__.py': b'', 'typing_inspection/typing_objects.py': TYPING_OBJECTS}

# A dependency that evaluates ForwardRefs: an attested (receipted) caller for annotationlib.
FWD = b"""
import annotationlib
def ev(ref, **ns):
    return ref.evaluate(globals=ns, locals=ns)
def make(text):
    return annotationlib.ForwardRef(text)
"""

PLUG = b"""
import annotationlib
def ev(text):
    ref = annotationlib.ForwardRef(text)
    ref.evaluate(globals={'list': list, 'int': int}, locals={})
    return ref
"""
'''


def run(tmp_path, body):
    script = tmp_path / "probe.py"
    script.write_text(PRELUDE + textwrap.dedent(body))
    result = subprocess.run([sys.executable, str(script), str(ROOT)], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", check=False,
                            env={"PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                                 "HERMES_HOME": str(tmp_path / "home")}, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_typing_inspection_producers_receipted_with_dependency_origin(tmp_path):  # tests 1 + A5
    result = run(tmp_path, r"""
        dist('typing_inspection', PKG)
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.typing_objects as t
        import typing
        assert t.is_any(typing.Any) and not t.is_any(1)
        class Box(typing.Generic[typing.TypeVar('T')]):
            pass
        assert t.is_generic(Box()) is True and t.is_generic(object()) is False
        units = generated()
        print(json.dumps({'new': new(), 'units': units, 'core': a.executed_units(),
                          'trust': a.dependency_receipts()['trust']}))
    """)
    assert result["new"] == []
    by = {u["generator"]: u for u in result["units"]}
    assert set(by) == {"typing-inspection:_compile_identity_check_function",
                       "typing-inspection:_compile_isinstance_check_function"}
    for unit in by.values():
        # A5: origin is the receipted typing_inspection module frame, not generated-origin-unknown.
        assert unit["component"] == "dependency:typing_inspection-1.0"
        assert unit["filename"] == "<string>" and unit["kind"] == "generated"
        assert unit["producer_distribution"]["dist"] == "typing-inspection"
        assert unit["producer_distribution"]["record_sha256"]
    assert result["core"] == []  # never an audited unit


def test_hash_mismatch_module_is_skew_and_producer_exec_unknown(tmp_path):  # R5-3 (i)
    result = run(tmp_path, r"""
        dist('typing_inspection', PKG)
        BASE = set(a.unknowns())
        (SITE / 'typing_inspection/typing_objects.py').write_bytes(TYPING_OBJECTS + b'\n# edited\n')
        activate()
        import typing_inspection.typing_objects
        print(json.dumps({'new': new(), 'gen': generated()}))
    """)
    assert any(u.startswith("dependency-skew:") and u.endswith("typing_objects.py")
               for u in result["new"]), result["new"]
    assert "unrooted-exec:<string>" in result["new"]
    assert result["gen"] == []


def test_exec_outside_the_table_is_unrooted(tmp_path):  # R5-3 (ii) + test 3 (A3 key)
    result = run(tmp_path, r"""
        dist('other_dist', {'typing_inspection/__init__.py': b'',
                            'typing_inspection/typing_objects.py': TYPING_OBJECTS})
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.typing_objects
        print(json.dumps({'new': new(), 'gen': generated()}))
    """)
    assert result["new"] == ["unrooted-exec:<string>"], result["new"]
    assert result["gen"] == []
    other = tmp_path / "samedist"
    other.mkdir()
    result = run(other, r"""
        dist('typing_inspection', {'typing_inspection/__init__.py': b'',
                                   'typing_inspection/other.py': TYPING_OBJECTS})
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.other
        print(json.dumps({'new': new(), 'gen': generated()}))
    """)
    assert result["new"] == ["unrooted-exec:<string>"], result["new"]


def test_missing_qualname_or_callsite_latches_callsite_missing(tmp_path):  # test 2 (A2)
    result = run(tmp_path, r"""
        only_one = TYPING_OBJECTS.split(b'def _compile_isinstance_check_function')[0]
        dist('typing_inspection', {'typing_inspection/__init__.py': b'',
                                   'typing_inspection/typing_objects.py': only_one})
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.typing_objects
        print(json.dumps({'new': new()}))
    """)
    assert ("generator-callsite-missing:typing-inspection:_compile_isinstance_check_function"
            in result["new"]), result["new"]
    assert not any(u.startswith("audit-error") for u in result["new"])
    tmp2 = tmp_path / "nocall"
    tmp2.mkdir()
    result = run(tmp2, r"""
        no_exec = TYPING_OBJECTS.replace(b'exec(func_code, globals_, locals_)', b'locals_[function_name] = None')
        dist('typing_inspection', {'typing_inspection/__init__.py': b'',
                                   'typing_inspection/typing_objects.py': no_exec})
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.typing_objects
        print(json.dumps({'new': new()}))
    """)
    assert ("generator-callsite-missing:typing-inspection:_compile_identity_check_function"
            in result["new"]), result["new"]
    assert not any(u.startswith("audit-error") for u in result["new"])


def test_forwardref_star_unpack_passes_and_bad_syntax_never_raises_from_hook(tmp_path):  # test 4
    result = run(tmp_path, r"""
        dist('fwdpkg', {'fwdpkg.py': FWD})
        BASE = set(a.unknowns())
        activate()
        import fwdpkg, typing, annotationlib
        Ts = typing.TypeVarTuple('Ts')
        got = fwdpkg.ev(fwdpkg.make('*Ts'), Ts=Ts)
        star_ok = new()
        ref = fwdpkg.make('list[int]')
        assert fwdpkg.ev(ref, list=list) == list[int]
        ok = new()
        bad = fwdpkg.make('1 +')
        try:
            fwdpkg.ev(bad)
            raised = None
        except SyntaxError as exc:
            raised = 'SyntaxError'
        after_raise = new()
        # code present but arg does not compile: the hook's own compile must not escape
        bad.__code__ = compile('1 + 1', '<string>', 'eval')
        fwdpkg.ev(bad)
        print(json.dumps({'star': star_ok, 'ok': ok, 'raised': raised, 'after_raise': after_raise,
                          'final': new(), 'is_unpack': got is not None}))
    """)
    assert result["star"] == [] and result["ok"] == []
    assert result["raised"] == "SyntaxError" and result["after_raise"] == []
    assert result["final"] == ["generated-code-mismatch:annotationlib.ForwardRef.evaluate"]


def test_swapped_code_object_is_mismatch(tmp_path):  # R5-3 (iii)
    result = run(tmp_path, r"""
        dist('fwdpkg', {'fwdpkg.py': FWD})
        BASE = set(a.unknowns())
        activate()
        import fwdpkg
        ref = fwdpkg.make('list[int]')
        ref.__code__ = compile('dict[int, int]', '<string>', 'eval')
        fwdpkg.ev(ref, list=list, dict=dict)
        print(json.dumps({'new': new()}))
    """)
    assert result["new"] == ["generated-code-mismatch:annotationlib.ForwardRef.evaluate"]


def test_unattested_caller_is_origin_unknown(tmp_path):  # R5-3 (iv)
    result = run(tmp_path, r"""
        import annotationlib
        BASE = set(a.unknowns())
        ref = annotationlib.ForwardRef('list[int]')
        ref.evaluate(globals={'list': list}, locals={})
        print(json.dumps({'new': new()}))
    """)
    assert len(result["new"]) == 1
    assert result["new"][0].startswith(
        "generated-origin-unknown:annotationlib.ForwardRef.evaluate:"), result["new"]


def test_non_forwardref_self_is_source_type(tmp_path):  # test 5 (B1)
    result = run(tmp_path, r"""
        import annotationlib
        BASE = set(a.unknowns())
        class Duck:
            __forward_arg__ = 'list[int]'
            __forward_code__ = compile('list[int]', '<string>', 'eval')
            def __getattr__(self, name):
                return None
        annotationlib.ForwardRef.evaluate(Duck(), globals={'list': list}, locals={})
        duck = new()
        out = {'duck': duck}
        try:
            class Sub(annotationlib.ForwardRef):
                pass
            sub = Sub('dict[int, int]')
            before = set(a.unknowns())
            sub.evaluate(globals={'dict': dict}, locals={})
            out['sub'] = sorted(set(a.unknowns()) - before)
        except TypeError:
            out['sub'] = 'final-class'
        print(json.dumps(out))
    """)
    assert result["duck"] == ["generated-source-type:annotationlib.ForwardRef.evaluate"]
    assert result["sub"] == "final-class" or \
        "generated-source-type:annotationlib.ForwardRef.evaluate" in result["sub"]


def test_repeated_evaluate_does_not_grow_units_and_remembers_nested_code(tmp_path):  # tests 6 + B4
    result = run(tmp_path, r"""
        p = pathlib.Path('plugin').resolve(); p.mkdir()
        src = PLUG
        (p / 'entry.py').write_bytes(src)
        BASE = set(a.unknowns())
        a.register_component_root('plug', p, 'plugin', digests={'entry.py': hashlib.sha256(src).hexdigest()})
        ns = {}
        exec(compile(src, str(p / 'entry.py'), 'exec'), ns)
        def fwd_units():
            return [u for u in a.executed_units() if u.get('generator') == 'annotationlib.ForwardRef.evaluate']
        ns['ev']('list[int]')
        one = len(fwd_units())
        for _ in range(50):
            ns['ev']('list[int]')
        fifty = len(fwd_units())
        ns['ev']('list[list[int]]')
        two = len(fwd_units())
        ref = ns['ev']('list[(lambda: int)()]')
        nested = [c for c in ref.__code__.co_consts if hasattr(c, 'co_code')]
        print(json.dumps({'one': one, 'fifty': fifty, 'two': two, 'new': new(),
                          'trust': fwd_units()[0].get('trust'),
                          'nested': len(nested), 'code_ok': a.attest_code(ref.__code__),
                          'nested_ok': all(a.attest_code(c) for c in nested)}))
    """)
    assert result["one"] == 1 and result["fifty"] == 1 and result["two"] == 2
    assert result["new"] == []
    assert "annotation expression text" in result["trust"]
    assert result["nested"] == 1 and result["code_ok"] and result["nested_ok"]


def test_module_reexec_replaces_descriptors(tmp_path):  # test 7 (A4)
    result = run(tmp_path, r"""
        dist('typing_inspection', PKG)
        BASE = set(a.unknowns())
        activate()
        import typing_inspection.typing_objects as t
        def live():
            return sorted(d.code.co_qualname for d in a._generators.values()
                          if d.name.startswith('typing-inspection:'))
        first = [id(d.code) for d in a._generators.values() if d.name.startswith('typing-inspection:')]
        importlib.reload(t)
        second = [id(d.code) for d in a._generators.values() if d.name.startswith('typing-inspection:')]
        print(json.dumps({'live': live(), 'same_ids': sorted(first) == sorted(second),
                          'count': len(second), 'new': new()}))
    """)
    assert result["live"] == ["_compile_identity_check_function", "_compile_isinstance_check_function"]
    assert result["count"] == 2 and result["new"] == []


def test_table_and_residual_guards():  # test 8 (R4-B)
    sys.path.insert(0, str(ROOT))
    import hermes_cli.code_attestation as ca
    assert ca._DEPENDENCY_GENERATORS == {
        ("typing-inspection", "typing_inspection/typing_objects.py"):
            ("_compile_identity_check_function", "_compile_isinstance_check_function")}
    text = (Path(__file__).parent / "test_code_attestation_deps.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*\w*RESIDUAL\w*\s*=", text, re.MULTILINE | re.IGNORECASE)  # no residual constant
    assert 'report["new"] == []' in text


def test_every_hook_compile_runs_under_busy(tmp_path):  # test 9
    result = run(tmp_path, r"""
        dist('typing_inspection', PKG)
        dist('fwdpkg', {'fwdpkg.py': FWD})
        BASE = set(a.unknowns())
        seen = []
        def spy(event, args):
            if event not in ('compile', 'exec'):
                return
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_filename == ca.__file__ and frame.f_code.co_name != '_audit':
                    seen.append((event, frame.f_code.co_name, bool(getattr(a._tls, 'busy', False))))
                    return
                frame = frame.f_back
        sys.addaudithook(spy)
        activate()
        import typing_inspection.typing_objects, fwdpkg
        fwdpkg.ev(fwdpkg.make('list[int]'), list=list)
        print(json.dumps({'seen': seen, 'new': new()}))
    """)
    assert result["new"] == []
    assert result["seen"], "hook-internal compiles must be observable for this check to mean anything"
    assert all(busy for _, _, busy in result["seen"]), result["seen"]
