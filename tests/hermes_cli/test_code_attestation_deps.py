"""R4 dependency-environment custody: installer RECORD receipts (trusted, not audited, no coverage).

Each case runs in a fresh subprocess with a real ``Attestation`` audit hook and a fabricated,
installer-shaped ``site-packages`` tree (``*.dist-info/RECORD`` with sha256 rows), registered the
way ``pm.environments.activate_dependencies`` does before ``site.addsitedir``.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRELUDE = r'''
import base64, hashlib, importlib, json, os, pathlib, site, subprocess, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.code_attestation import Attestation
root = pathlib.Path(sys.argv[1])
a = Attestation(root, subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                              text=True).strip())
SITE = pathlib.Path('env/lib/python3/site-packages').resolve()

def b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()

def dist(name, files, *, record=True, rows=None):
    """Write files into SITE and a RECORD naming them (rows overrides per-path rows)."""
    SITE.mkdir(parents=True, exist_ok=True)
    lines = []
    for rel, data in files.items():
        path = SITE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        lines.append((rows or {}).get(rel, f'{rel},sha256={b64(data)},{len(data)}'))
    info = SITE / f'{name}-1.0.dist-info'
    info.mkdir()
    (info / 'INSTALLER').write_text('uv\n')
    if record:
        lines.append(f'{name}-1.0.dist-info/RECORD,,')
        (info / 'RECORD').write_text('\n'.join(l for l in lines if l is not None) + '\n')

def new():
    return sorted(set(a.unknowns()) - BASE)
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


def test_receipted_dependency_is_reported_never_audited(tmp_path):
    result = run(tmp_path, r"""
        dist('goodpkg', {'goodpkg/__init__.py':
            b'import collections\nPoint = collections.namedtuple("Point", "x y")\nVALUE = 7\n'})
        BASE = set(a.unknowns())
        assert a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        import goodpkg
        assert goodpkg.VALUE == 7 and goodpkg.Point(1, 2).y == 2
        print(json.dumps({'new': new(), 'deps': a.dependency_receipts(),
                          'units': a.executed_units()}))
    """)
    assert result["new"] == []
    kinds = {(Path(u["filename"]).name, u["kind"]) for u in result["deps"]["units"]}
    assert ("__init__.py", "source") in kinds and ("<string>", "generated") in {
        (u["filename"].rsplit("/", 1)[-1], u["kind"]) for u in result["deps"]["units"]} | kinds
    assert "goodpkg-1.0" in result["deps"]["dists"]
    assert result["deps"]["trust"] == "installer-receipt: trusted, not audited, no coverage"
    assert not [u for u in result["units"] if "site-packages" in u.get("filename", "")
                or u["component"].startswith("dependency:")]


def test_tampered_missing_row_size_and_escaping_rows_refuse(tmp_path):
    result = run(tmp_path, r"""
        data = b'VALUE = 1\n'
        dist('skewpkg', {'skewpkg.py': data})
        dist('norow', {'norow.py': b'VALUE = 2\n'}, rows={'norow.py': None})
        dist('badsize', {'badsize.py': b'VALUE = 3\n'},
             rows={'badsize.py': f'badsize.py,sha256={b64(b"VALUE = 3\n")},999'})
        dist('escape', {'escape.py': b'VALUE = 4\n'},
             rows={'escape.py': f'../site-packages/escape.py,sha256={b64(b"VALUE = 4\n")},10'})
        dist('norecord', {'norecord.py': b'VALUE = 5\n'}, record=False)
        BASE = set(a.unknowns())
        a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        (SITE / 'skewpkg.py').write_bytes(b'VALUE = 9\n')  # edited in place after install
        for name in ('skewpkg', 'norow', 'badsize', 'escape', 'norecord'):
            importlib.import_module(name)
        print(json.dumps({'new': new()}))
    """)
    new = result["new"]
    assert any(u.startswith("dependency-skew:") and u.endswith("skewpkg.py") for u in new), new
    assert any(u.startswith("dependency-skew:") and u.endswith("badsize.py") for u in new), new
    for name in ("norow", "escape", "norecord"):
        assert any(u.startswith("dependency-unreceipted:") and u.endswith(name + ".py")
                   for u in new), (name, new)


def test_owner_precedence_plugin_root_inside_site_packages(tmp_path):
    result = run(tmp_path, r"""
        dist('plugpkg', {'plugpkg/__init__.py': b'VALUE = 1\n'})
        BASE = set(a.unknowns())
        a.register_dependency_environment(SITE)
        a.register_component_root('plug', SITE / 'plugpkg', 'plugin',
                                  digests={'other.py': 'x'})
        site.addsitedir(str(SITE))
        import plugpkg
        print(json.dumps({'new': new(), 'deps': a.dependency_receipts()['units']}))
    """)
    assert any(u.startswith("source-skew:") and "plugpkg" in u for u in result["new"]), result
    assert not [u for u in result["deps"] if "plugpkg" in u["filename"]]


def test_dependency_own_exec_stays_unknown(tmp_path):
    result = run(tmp_path, r"""
        dist('execpkg', {'execpkg.py': b'exec("x = 1")\n'})
        BASE = set(a.unknowns())
        a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        import execpkg
        print(json.dumps({'new': new()}))
    """)
    assert result["new"] == ["unrooted-exec:<string>"]


def test_receipted_pth_import_line_is_receipted_and_unreceipted_pth_refuses(tmp_path):
    result = run(tmp_path, r"""
        dist('hookpkg', {'hookpkg_hook.py': b'LOADED = True\n',
                         'hookpkg.pth': b'import hookpkg_hook\n'})
        BASE = set(a.unknowns())
        a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        assert sys.modules['hookpkg_hook'].LOADED
        good = new()
        kinds = sorted(u['kind'] for u in a.dependency_receipts()['units'])
        (SITE / 'rogue.pth').write_text('import os\n')
        BASE2 = set(a.unknowns())
        a.register_dependency_environment(SITE)
        site.addpackage(str(SITE), 'rogue.pth', set())
        print(json.dumps({'good': good, 'kinds': kinds, 'bad': sorted(set(a.unknowns()) - BASE2)}))
    """)
    assert result["good"] == []
    assert {"pth", "pth-exec", "source"} <= set(result["kinds"])
    assert "pth-unreceipted:rogue.pth" in result["bad"]
    assert any(u.startswith("pth-exec-unreceipted:") for u in result["bad"]), result["bad"]


def test_native_loads_are_finite_and_caller_bound(tmp_path):
    result = run(tmp_path, r"""
        dist('nat', {'natpkg.py': b'import ctypes\nLIBC = ctypes.CDLL(None)\n'
                                  b'def other():\n    return ctypes.CDLL("libm.so.6")\n'})
        BASE = set(a.unknowns())
        a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        import natpkg
        allowed = new()
        natpkg.other()
        other = new()
        p = pathlib.Path('plugin').resolve(); p.mkdir()
        src = b'import ctypes\nctypes.CDLL(None)\n'
        (p / 'entry.py').write_bytes(src)
        a.register_component_root('plug', p, 'plugin',
                                  digests={'entry.py': hashlib.sha256(src).hexdigest()})
        exec(compile(src, str(p / 'entry.py'), 'exec'), {})
        print(json.dumps({'allowed': allowed, 'other': other, 'plugin': new(),
                          'loads': a.dependency_receipts()['native_loads']}))
    """)
    assert result["allowed"] == []
    assert {"name": None, "caller_component": "dependency:nat-1.0"} in result["loads"]
    assert "native-load:libm.so.6" in result["other"]
    assert "native-load:None" in result["plugin"]


def test_environment_registration_and_index_limit_fail_closed(tmp_path):
    result = run(tmp_path, r"""
        import hermes_cli.code_attestation as ca
        BASE = set(a.unknowns())
        other = pathlib.Path('not-a-site').resolve(); other.mkdir()
        assert not a.register_dependency_environment(other)
        rejected = new()
        ca.MAX_DISTS = 1
        dist('one', {'one.py': b'A = 1\n'})
        dist('two', {'two.py': b'B = 2\n'})
        a.register_dependency_environment(SITE)
        site.addsitedir(str(SITE))
        import one
        print(json.dumps({'rejected': rejected, 'limit': new()}))
    """)
    assert result["rejected"] == [f"dependency-environment-rejected:{tmp_path / 'not-a-site'}"] or \
        result["rejected"][0].startswith("dependency-environment-rejected:")
    assert "dependency-index-limit" in result["limit"]


# R4 step 8 / R4-B: the real gateway runtime import graph on the supported target's shape
# (base interpreter, committed site-packages activated post-hook by core). No residual list.
GATEWAY_GRAPH = ("gateway.run", "gateway.quiet_control", "tui_gateway.server",
                 "agent.auxiliary_client", "tools.mcp_tool", "run_agent")


def test_gateway_runtime_import_graph_has_no_residual_unknowns(tmp_path):
    selected = next(Path(p) for p in sys.path if Path(p).name == "site-packages")
    script = tmp_path / "graph.py"
    script.write_text(textwrap.dedent(r'''
        import importlib, json, pathlib, site, subprocess, sys, threading
        root, selected = pathlib.Path(sys.argv[1]), sys.argv[2]
        sys.path.insert(0, str(root))
        from hermes_cli.code_attestation import Attestation
        a = Attestation(root, subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                                      text=True).strip())
        BASE = set(a.unknowns())
        # pm.environments.activate_dependencies order: register, then addsitedir.
        assert a.register_dependency_environment(selected)
        site.addsitedir(selected)
        failed = {}
        for name in sys.argv[3:]:
            try:
                importlib.import_module(name)
            except Exception as exc:
                failed[name] = repr(exc)
        pathlib.Path("graph.json").write_text(json.dumps({"new": sorted(set(a.unknowns()) - BASE), "failed": failed,
                          "producers": a.dependency_receipts()["unknown_producers"],
                          "generated": [(u["generator"], u["component"]) for u in a.dependency_receipts()["units"]
                                        if u["kind"] == "generated"],
                          "threads": sorted(t.name for t in threading.enumerate()),
                          "dists": len(a.dependency_receipts()["dists"]),
                          "known": len(a._known)}))
    '''))
    result = subprocess.run([sys._base_executable, "-I", str(script), str(ROOT), str(selected),
                             *GATEWAY_GRAPH], cwd=tmp_path, capture_output=True, text=True,
                            encoding="utf-8", check=False, timeout=240,
                            env={"PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                                 "HERMES_HOME": str(tmp_path / "home")})
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    report = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    assert report["failed"] == {}, report["failed"]
    assert report["dists"] > 10
    # The executed-code index must hold the whole runtime graph with room for plugins and
    # providers loaded later; hitting the cap latches loaded-code-limit and fails every start.
    from hermes_cli.code_attestation import MAX_LOADED_CODE
    assert report["known"] * 2 < MAX_LOADED_CODE, (report["known"], MAX_LOADED_CODE)
    assert report["new"] == [], (report["new"], report["producers"])
    # R5: the real producers were receipted with a dependency origin, not latched unknown.
    generators = {g for g, _ in report["generated"]}
    assert {"typing-inspection:_compile_identity_check_function",
            "typing-inspection:_compile_isinstance_check_function"} <= generators, generators
    assert all(c.startswith("dependency:") for g, c in report["generated"]
               if g.startswith("typing-inspection:")), report["generated"]
