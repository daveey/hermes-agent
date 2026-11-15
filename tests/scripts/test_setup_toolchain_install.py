"""Install-boundary coverage for toolchain intent versus transitive Python."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci import setup_toolchain


@pytest.mark.parametrize("toolchain,extra", [("node", ["ripgrep"]), ("python", []), ("all", [])])
def test_install_creates_command_environment_only_for_uv(toolchain, extra, tmp_path, monkeypatch):
    from pm.lock import Facts
    from pm.registry import get_package, walk
    from pm.store import current_target

    home = tmp_path / "home"
    home.mkdir()
    store = home / "tools"
    facts_file = home / "facts.json"
    monkeypatch.setattr("pm.paths.store_root", lambda: store)
    monkeypatch.setattr("pm.paths.facts_path", lambda: facts_file)
    installed = set()

    def ensure(name, **kwargs):
        # Model only artifact acquisition; use the real dependency closure and Facts.
        facts = Facts(facts_file)
        for package in walk([name]):
            installed.add(package.name)
            facts.record(package.name, "1", package.name, {}, store)

    monkeypatch.setattr("pm.install.ensure", ensure)
    monkeypatch.setattr("pm.install.env_for", lambda *names, **kwargs: {"PATH": str(store / "bin")})
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: str(home / "npm-cache"))
    monkeypatch.setattr("pm.packages.uv_cache_dir", lambda: home / "uv-cache")
    builds = []

    def build(requirements, *, out, python, **kwargs):
        builds.append((requirements, out, python))
        out.mkdir(parents=True)
        (out / "pyvenv.cfg").touch()

    monkeypatch.setattr("pm.build_requirements_environment", build)
    monkeypatch.setattr(setup_toolchain, "python3_alias", lambda python: None)
    files = {name: tmp_path / name for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH")}
    for name, path in files.items():
        monkeypatch.setenv(name, str(path))
    args = SimpleNamespace(home=home, toolchain=toolchain, packages=extra)
    setup_toolchain.install(args)
    outputs = dict(line.split("=", 1) for line in files["GITHUB_OUTPUT"].read_text().splitlines())
    exports = dict(line.split("=", 1) for line in files["GITHUB_ENV"].read_text().splitlines())
    paths = files["GITHUB_PATH"].read_text().splitlines()
    assert installed == set(setup_toolchain.packages(toolchain, extra))
    managed = "uv" in installed
    assert bool(builds) == managed
    assert ("uv-path" in outputs) == managed
    assert ("uv-cache-path" in outputs) == managed
    assert ("HERMES_PYTHON" in exports) == managed
    raw_python = get_package("python").binary(store / "python", current_target())
    if managed:
        assert builds == [([], home / "python" / "python", raw_python)]
        assert outputs["python-path"] == exports["HERMES_PYTHON"]
        assert str(Path(outputs["python-path"]).parent) == paths[-1]
        assert str(Path(outputs["uv-path"]).parent) in paths
        setup_toolchain.install(args)
        assert len(builds) == 1, "warm command environment must be reused"
    else:
        assert outputs["python-path"] == str(raw_python)
        assert "ripgrep-path" in outputs
        assert not (home / "python").exists()
    if toolchain != "python":
        assert outputs["npm-cache-path"] == str(home / "npm-cache")
        assert outputs["pnpm-store-path"] == exports["pnpm_config_store_dir"]
