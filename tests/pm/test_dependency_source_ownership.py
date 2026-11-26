"""Duplicate installed wheels cannot steal committed dependency ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


def _distribution(root, record="ownership_probe/yaml/__init__.py,,\n", *, editable=False):
    info = root / "ownership_probe-1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: ownership-probe\nVersion: 1\n")
    (info / "RECORD").write_text(record)
    if editable:
        (info / "direct_url.json").write_text(json.dumps({"dir_info": {"editable": True}}))
    return info


@pytest.mark.parametrize("mutate_namespace_path", [False, True])
def test_activation_uses_managed_duplicate_without_stealing_namespace(tmp_path, monkeypatch, mutate_namespace_path):
    from pm import environments

    root = tmp_path / "repo"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = environments.install_state_dir(root)
    selected = state / "environments" / "selected" / "venv"
    site = environments.site_packages(selected)
    site.mkdir(parents=True)
    root.mkdir()
    (selected / "pyvenv.cfg").write_text("home = fixture\n")
    (state / "facts.json").write_text(json.dumps({"schema": 1, "packages": {
        "venv": {"environment": str(selected)},
    }}))
    for parent, value in ((root, "stray"), (site, "managed")):
        package = parent / "ownership_probe" / "yaml"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"value = {value!r}\n")
        _distribution(parent)
    # A legitimate source portion of the same namespace must remain importable.
    (root / "ownership_probe/source_member.py").write_text("value = 'source'\n")
    before = (root / "ownership_probe/yaml/__init__.py").read_bytes()
    code = (
        "import sys; from pathlib import Path; "
        "from pm.environments import activate_dependencies; "
        "activate_dependencies(Path(sys.argv[1])); "
        + ("import ownership_probe; sys.path.insert(0, sys.argv[1]); " if mutate_namespace_path else "") +
        "import ownership_probe.yaml as yaml; from ownership_probe import source_member; "
        "print(yaml.value); print(yaml.__file__); print(source_member.value)"
    )
    process = subprocess.run([sys.executable, "-c", code, str(root)],
                             env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert process.returncode == 0, process.stderr
    assert process.stdout.splitlines() == ["managed", str(site / "ownership_probe/yaml/__init__.py"), "source"]
    assert (root / "ownership_probe/yaml/__init__.py").read_bytes() == before


@pytest.mark.parametrize("record,expected", [
    ("ordinary.py,,\n", {"ordinary": "."}),
    ("native.cpython-314-x86_64-linux-gnu.so,,\n", {"native": "."}),
    ("native.cp314-win_amd64.pyd,,\n", {"native": "."}),
    ("namespace/member/__init__.py,,\n", {"namespace.member": "namespace"}),
    ("../outside.py,,\n/absolute.py,,\nnot-a-module.py,,\n", {}),
    ("package-1.data/scripts/command.py,,\npackage-1.dist-info/METADATA,,\n", {}),
    ("\nassets/logo.png,,\n", {}),
])
def test_only_recorded_importable_modules_are_owned(tmp_path, record, expected):
    from pm.dependency_imports import _recorded_modules

    assert _recorded_modules(_distribution(tmp_path, record)) == expected


@pytest.mark.parametrize("editable", [False, True])
def test_source_and_editable_ownership_survive(tmp_path, editable):
    from pm.dependency_imports import _duplicate_modules

    root, selected = tmp_path / "source", tmp_path / "selected"
    # A managed wheel claiming tests does not take source precedence by itself.
    _distribution(selected, "tests/__init__.py,,\n", editable=editable)
    assert _duplicate_modules(root, selected) == {}
    _distribution(root, "tests/__init__.py,,\n")
    owners = _duplicate_modules(root, selected)
    assert owners == ({} if editable else {"tests": str(selected)})


def test_distribution_identity_and_exact_module_intersection(tmp_path):
    from pm.dependency_imports import _duplicate_modules

    root, selected = tmp_path / "source", tmp_path / "selected"
    root_info = _distribution(root, "ownership_probe/yaml/__init__.py,,\nother_source.py,,\n")
    _distribution(selected, "ownership_probe/yaml/__init__.py,,\nmanaged_only.py,,\n")
    assert _duplicate_modules(root, selected) == {"ownership_probe.yaml": str(selected / "ownership_probe")}
    (root_info / "METADATA").write_text("Name: unrelated\nVersion: 1\n")
    assert _duplicate_modules(root, selected) == {}


def test_missing_owned_module_refuses_source_fallback(tmp_path):
    from pm.dependency_imports import _DuplicateWheelFinder

    finder = _DuplicateWheelFinder({"ownership_probe.yaml": str(tmp_path)})
    with pytest.raises(ModuleNotFoundError, match="selected dependency source missing") as failure:
        finder.find_spec("ownership_probe.yaml", ["stray"])
    assert failure.value.name == "ownership_probe.yaml"
    assert finder.find_spec("ownership_probe") is None
    assert finder.find_spec("core_source") is None


def test_loaded_checkout_code_refuses_instead_of_eviction(tmp_path, monkeypatch):
    from pm.dependency_imports import select_wheel_sources

    root, selected = tmp_path / "source", tmp_path / "selected"
    _distribution(root)
    _distribution(selected)
    foreign = ModuleType("ownership_probe.yaml")
    foreign.__file__ = str(root / "ownership_probe/yaml/__init__.py")
    monkeypatch.setitem(sys.modules, "ownership_probe.yaml", foreign)
    with pytest.raises(RuntimeError, match="already imported before PM activation"):
        select_wheel_sources(root, selected)
    assert sys.modules["ownership_probe.yaml"] is foreign


def test_repeated_install_preserves_builtin_and_foreign_finders(tmp_path, monkeypatch):
    from importlib.machinery import BuiltinImporter, FrozenImporter, PathFinder
    from pm.dependency_imports import _DuplicateWheelFinder, select_wheel_sources

    root, selected = tmp_path / "source", tmp_path / "selected"
    _distribution(root)
    _distribution(selected)
    sentinel = object()
    monkeypatch.setattr(sys, "meta_path", [BuiltinImporter, FrozenImporter, sentinel, PathFinder])
    select_wheel_sources(root, selected)
    select_wheel_sources(root, selected)
    assert sys.meta_path[:3] == [BuiltinImporter, FrozenImporter, sentinel]
    assert isinstance(sys.meta_path[3], _DuplicateWheelFinder)
    assert sys.meta_path[4:] == [PathFinder]
