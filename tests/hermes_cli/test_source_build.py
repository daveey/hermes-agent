"""Source orchestration uses real node-deps/pnpm in an isolated checkout.

Only PM's tool acquisition (and its installed-node record) is substituted with the host's node/pnpm. Small
workspace scripts stand in for the expensive UI compilers; subprocess failures,
locked dependency selection, environment propagation and publication are real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import pm
from pm.package import Runner


def use_host_node_as_pm_node(monkeypatch):
    """Freshness reads run only under PM's recorded Node; stand the host's node in for it."""
    node, real = Path(shutil.which("node")), pm.installed_package
    monkeypatch.setattr(pm, "installed_package", lambda name, **kwargs: (
        SimpleNamespace(binary=node) if name == "node" else real(name, **kwargs)))


def copy_freshness_scripts(root):
    repository = Path(__file__).resolve().parents[2]
    scripts = root / "scripts/build"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in ("freshness.mjs", "frontend-common.mjs"):
        shutil.copy2(repository / "scripts/build" / name, scripts / name)


def stamp_product(root, product, out):
    script = (root / "scripts/build/freshness.mjs").as_uri()
    subprocess.run([shutil.which("node"), "--input-type=module", "-e",
                    f"import {{recordProduct, buildInputs}} from {json.dumps(script)};"
                    "const [source, product, out] = process.argv.slice(1);"
                    "recordProduct({source, product, out, inputs: buildInputs(source, product)});",
                    str(root), product, str(out)], check=True)


def test_source_build_uses_selected_python_for_isolated_icon_child(tmp_path, monkeypatch):
    from hermes_cli.source_build import source_build_env
    from pm import paths
    from pm.environments import site_packages, venv_python

    root = tmp_path / "source"
    root.mkdir()
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    selected = site_packages(venv)
    (selected / "icon_dependency.py").write_text("ready = True\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: root)
    monkeypatch.setattr(pm, "ensure", lambda name, **kwargs: Runner(name, kwargs["base_env"]))
    monkeypatch.syspath_prepend(str(selected))

    env = source_build_env()
    assert env["HERMES_PYTHON"] == str(venv_python(venv))
    subprocess.run([env["HERMES_PYTHON"], "-I", "-c",
                    "import icon_dependency; assert icon_dependency.ready"], check=True)


def test_automatic_build_preserves_pm_admission_intent(monkeypatch):
    from hermes_cli.source_build import source_build_env

    intent = []
    def acquire(name, *, base_env, explicit, verify=True):
        intent.append(explicit)
        return Runner(name, base_env)
    monkeypatch.setattr(pm, "ensure", acquire)
    source_build_env()
    assert intent == [False]


def test_installed_pnpm_does_not_authorize_missing_workspace_dependencies(source_checkout, monkeypatch):
    from hermes_cli.source_build import prepare_source_dependencies, source_build_env

    root, _ = source_checkout
    env = source_build_env()
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    with pytest.raises(subprocess.CalledProcessError):
        prepare_source_dependencies(root, ("ui-tui", "web"), env=env)
    assert _events(root) == []
    prepare_source_dependencies(root, ("ui-tui", "web"), env=env, explicit=True)
    events = _events(root)
    prepare_source_dependencies(root, ("ui-tui", "web"), env=env)
    assert _events(root) == events


@pytest.fixture
def source_checkout(tmp_path, monkeypatch):
    node, pnpm = shutil.which("node"), shutil.which("pnpm")
    assert node and pnpm, "source-build integration requires node and pnpm"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "0")
    monkeypatch.setenv("ESBUILD_BINARY_PATH", "/wrong/esbuild")
    monkeypatch.setenv("HERMES_PYTHON", "/wrong/python")
    (home / "npmrc").write_text(
        "fund=false\nnode-get-windows-binary-host-mirror=https://mirror.invalid/get-windows/\n"
        "//registry.invalid/:_authToken=npmrc-secret\nelectron-mirror=${MIRROR_BASE}/electron/\n"
        "@scope:registry=https://registry.invalid/\nca[]=npmrc-array\nnodedir=${UNSET_NPMRC_VAR}\n",
        encoding="utf-8")
    monkeypatch.setenv("MIRROR_BASE", "https://mirror.invalid")
    monkeypatch.delenv("UNSET_NPMRC_VAR", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    acquired = []

    def acquire(name, *, base_env=None, explicit=False, verify=True):
        acquired.append(name)
        assert name == "pnpm"

        return Runner(name, {**(base_env or os.environ), "PATH": os.pathsep.join(
            [str(Path(pnpm).parent), str(Path(node).parent), os.environ["PATH"]])})

    monkeypatch.setattr(pm, "ensure", acquire)
    use_host_node_as_pm_node(monkeypatch)
    root = tmp_path / "source with spaces"
    root.mkdir()
    workspaces = ["ui-tui", "web", "apps/desktop", "unrelated"]
    manifest = {"name": "build-fixture", "private": True, "version": "1.0.0",
                "scripts": {"postinstall": "node log.mjs deps"}}
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    # verifyDepsBeforeRun mirrors the repository's: `pnpm run` never installs. It precedes the
    # package list, which tests extend by appending.
    (root / "pnpm-workspace.yaml").write_text(
        "verifyDepsBeforeRun: false\npackages:\n" + "".join(f"  - {workspace}\n" for workspace in workspaces),
        encoding="utf-8")
    # A local package stands in for registry dependencies: pnpm links it into every installed
    # workspace's node_modules, offline.
    (root / "fixture-dep").mkdir()
    (root / "fixture-dep/package.json").write_text('{"name": "fixture-dep", "version": "1.0.0"}', encoding="utf-8")
    for workspace in workspaces:
        directory = root / workspace
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({
            "name": workspace.replace("/", "-"), "version": "1.0.0",
            "dependencies": {"fixture-dep": f"file:{os.path.relpath(root / 'fixture-dep', directory)}"},
            "scripts": {"build": "node ../../scripts/build/build-desktop.mjs",
                        "builder": "node ../../scripts/build/package-desktop.mjs"}
            if workspace == "apps/desktop" else {},
        }), encoding="utf-8")
    (root / "log.mjs").write_text(
        "import { appendFileSync } from 'node:fs';\n"
        "appendFileSync('events.jsonl', JSON.stringify({step: process.argv[2], "
        "python: process.env.HERMES_PYTHON, ci: process.env.CI, "
        "esbuild: process.env.ESBUILD_BINARY_PATH, "
        "npmrc: process.env.NPM_CONFIG_USERCONFIG, store: process.env.pnpm_config_store_dir, "
        "mirror: process.env.npm_config_node_get_windows_binary_host_mirror, "
        "electron: process.env.npm_config_electron_mirror, retries: process.env.pnpm_config_fetch_retries, "
        "skipped: Object.keys(process.env).filter(key => /^npm_config_(@.*|ca(\\[\\])?|nodedir)$/i.test(key)), "
        "leaked: Object.values(process.env).some(value => value.includes('npmrc-secret'))}) + '\\n');\n",
        encoding="utf-8",
    )
    subprocess.run([pnpm, "install", "--lockfile-only", "--ignore-scripts", "--offline"], cwd=root, check=True,
                   env={**os.environ, **{f"pnpm_config_{name}_dir": str(tmp_path / "pnpm" / name)
                                         for name in ("store", "cache", "state")}})
    scripts = root / "scripts" / "build"
    scripts.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[2]
    for name in ("node-deps*.mjs", "electron-runtime.mjs", "freshness.mjs", "frontend-common.mjs", "pnpm-lock.mjs"):
        for script in (repository / "scripts/build").glob(name):
            shutil.copy2(script, scripts / script.name)
    (root / ".gitignore").write_text("node_modules/\n**/dist/\n", encoding="utf-8")
    # node-deps requires PM's pnpm pin: pin the pnpm this test runs.
    (root / "pm").mkdir()
    version = subprocess.run([pnpm, "--version"], capture_output=True, text=True, encoding="utf-8",
                             timeout=60, check=True).stdout.strip()
    (root / "pm/lock.json").write_text(json.dumps({"packages": {"pnpm": {"version": version}}}), encoding="utf-8")
    return root, acquired


@pytest.fixture
def source_products(source_checkout, monkeypatch):
    # Product builds must not enumerate or stop the host's real desktop processes.
    # Keep the swap/install path real; process-table behavior has separate desktop tests.
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    root, acquired = source_checkout
    (root / "product.mjs").write_text(
        "import { appendFileSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';\n"
        "import { dirname, join } from 'node:path';\n"
        "import { fileURLToPath } from 'node:url';\n"
        "import { recordProduct, buildInputs } from './scripts/build/freshness.mjs';\n"
        "const root = dirname(fileURLToPath(import.meta.url));\n"
        "export function build(step, output) {\n"
        "  appendFileSync(join(root, 'events.jsonl'), JSON.stringify({step}) + '\\n');\n"
        "  if (existsSync(join(root, 'fail-' + step))) throw new Error('fixture ' + step + ' failure');\n"
        "  if (step === 'web' && !existsSync(join(root, 'web/public/favicon.ico'))) throw new Error('icons missing');\n"
        "  const path = join(root, output); mkdirSync(dirname(path), { recursive: true });\n"
        "  writeFileSync(path, step);\n"
        "  if (step !== 'icons') {\n"
        "    let out = dirname(path);\n"
        "    if (step === 'desktop') { out = join(out, 'resources/app.asar.unpacked/dist');\n"
        "      mkdirSync(out, {recursive: true}); writeFileSync(join(out, 'index.html'), 'renderer'); }\n"
        "    recordProduct({source: root, product: step, out, inputs: buildInputs(root, step)});\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    for script, step, output in [
        ("generate-icons.mjs", "icons", "never-rendered-at-install"),
        ("build/tui.mjs", "tui", "ui-tui/dist/entry.js"),
        ("build/web.mjs", "web", "hermes_cli/web_dist/index.html"),
    ]:
        relative = "../../" if script.startswith("build/") else "../"
        (root / "scripts" / script).write_text(
            f"import {{ build }} from '{relative}product.mjs'; build({step!r}, {output!r});\n",
            encoding="utf-8",
        )
    (root / "scripts/build/package-desktop.mjs").write_text(
        "import { relative } from 'node:path';\n"
        "import { build } from '../../product.mjs';\n"
        "const flag = '-c.directories.output=';\n"
        "const staging = process.argv.find(arg => arg.startsWith(flag)).slice(flag.length);\n"
        "build('desktop', relative('../..', staging) + '/linux-unpacked/hermes');\n",
        encoding="utf-8",
    )
    (root / "scripts/build/build-desktop.mjs").write_text(
        "if (!process.argv.includes('--icons')) await import('../generate-icons.mjs');\n",
        encoding="utf-8",
    )
    # Default-brand icons are committed; updates consume them without rendering.
    (root / "web/public").mkdir(parents=True, exist_ok=True)
    (root / "web/public/favicon.ico").write_bytes(b"committed icon")
    return root, acquired


def _events(root):
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.platforms("posix")
def test_preparation_reuses_only_the_exact_completed_workspace_union(source_checkout):
    from hermes_cli.source_build import prepare_source_dependencies, source_build_env

    root, acquired = source_checkout
    before = (root / "pnpm-lock.yaml").read_bytes()
    env = source_build_env()
    prepare_source_dependencies(root, ("ui-tui", "web"), env=env)
    first = _events(root)
    assert [event["step"] for event in first] == ["deps"]
    assert first[0]["python"] == env["HERMES_PYTHON"]
    assert first[0]["ci"] == "1"
    assert "esbuild" not in first[0]
    assert first[0]["npmrc"] == str(Path(os.environ["HERMES_HOME"]) / "npmrc")
    # npmrc settings reach install scripts as npm did it (node-pre-gyp mirrors); credentials never do.
    assert first[0]["mirror"] == "https://mirror.invalid/get-windows/"
    assert first[0]["electron"] == "https://mirror.invalid/electron/"
    assert first[0]["skipped"] == []
    assert first[0]["leaked"] is False
    # Updates and builds keep pnpm's default download retries; only a launch lowers them.
    assert "retries" not in first[0]
    # pnpm's store lives under the Hermes root, never the user's XDG data dir.
    assert first[0]["store"] == str(Path(os.environ["HERMES_HOME"]) / "cache/pnpm/store")
    assert (Path(os.environ["HERMES_HOME"]) / "cache/pnpm/store").is_dir()
    assert (root / "ui-tui/node_modules/fixture-dep").exists()
    assert (root / "web/node_modules/fixture-dep").exists()
    assert not (root / "apps/desktop/node_modules").exists()
    assert not (root / "unrelated/node_modules").exists()

    prepare_source_dependencies(root, ("ui-tui", "web"), env=env)
    assert _events(root) == first
    prepare_source_dependencies(root, ("ui-tui", "web", "apps/desktop"), env=env)
    assert len(_events(root)) == 2
    assert (root / "apps/desktop/node_modules/fixture-dep").exists()
    assert not (root / "unrelated/node_modules").exists()
    assert (root / "pnpm-lock.yaml").read_bytes() == before
    assert acquired == ["pnpm"]

    # A lock failure must not fall back to an unlocked install that rewrites the lock.
    (root / "pnpm-lock.yaml").write_text("not yaml", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        prepare_source_dependencies(root, ("ui-tui", "web"), env=env)
    assert (root / "pnpm-lock.yaml").read_text() == "not yaml"
    assert len(_events(root)) == 2
    assert acquired == ["pnpm"]

    # Exercise PM rather than hiding a provisioning error behind system pnpm.
    from unittest.mock import patch
    with patch.object(pm, "ensure", side_effect=pm.InstallError("pnpm", "unavailable")):
        with pytest.raises(pm.InstallError, match="unavailable"):
            source_build_env()
    assert len(_events(root)) == 2


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("desktop", [False, True])
def test_update_builds_selected_products_after_one_union_preparation(source_products, desktop):
    from hermes_cli.source_build import build_update_products
    from hermes_cli.main_web_build import _web_ui_build_needed

    root, acquired = source_products
    app = root / "apps/desktop/release/linux-unpacked/hermes"
    app.parent.mkdir(parents=True)
    app.write_text("previous app")
    build_update_products(root, desktop=desktop)
    steps = [event["step"] for event in _events(root)]
    assert steps == ["deps", "tui", "web"] + (["desktop"] if desktop else [])
    assert acquired == ["pnpm"]
    assert (root / "ui-tui/dist/entry.js").read_text() == "tui"
    assert (root / "hermes_cli/web_dist/index.html").read_text() == "web"
    assert not _web_ui_build_needed(root / "web")
    assert (root / "apps/desktop/node_modules").exists() == desktop
    assert not (root / "unrelated/node_modules").exists()
    assert app.read_text() == ("desktop" if desktop else "previous app")
    assert not list((root / "apps/desktop").glob(".staging-*"))


@pytest.mark.platforms("linux")
def test_update_recompiles_only_products_whose_inputs_changed(source_products):
    from hermes_cli.source_build import build_update_products

    root, _ = source_products

    def products():
        return [event["step"] for event in _events(root) if event["step"] != "deps"]

    build_update_products(root, desktop=True)
    (root / "events.jsonl").unlink()
    build_update_products(root, desktop=True)
    assert products() == ["desktop"]

    (root / "events.jsonl").unlink()
    (root / "web/src").mkdir(parents=True, exist_ok=True)
    (root / "web/src/changed.ts").write_text("export {}\n", encoding="utf-8")
    build_update_products(root, desktop=False)
    assert products() == ["web"]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("step", ["tui", "web", "desktop"])
def test_update_failure_raises_without_retries_or_replacing_live_app(source_products, step):
    from hermes_cli.source_build import ProductBuildError, build_update_products

    root, acquired = source_products
    app = root / "apps/desktop/release/linux-unpacked/hermes"
    app.parent.mkdir(parents=True)
    app.write_text("previous app")
    (root / f"fail-{step}").touch()
    with pytest.raises(ProductBuildError) as failure:
        build_update_products(root, desktop=True)
    # The failure is still raised, naming the one product that failed (no retries) ...
    assert len(failure.value.failures) == 1
    assert isinstance(failure.value.failures[0][1], subprocess.CalledProcessError)
    # ... but it no longer skips the independent products after it (was order[:index + 1]).
    assert [event["step"] for event in _events(root)] == ["deps", "tui", "web", "desktop"]
    assert acquired == ["pnpm"]
    assert not list((root / "apps/desktop").glob(".staging-*"))
    if step == "desktop":
        # A failed desktop build never replaces the live app nor stamps it.
        assert app.read_text() == "previous app"
        assert not (Path(os.environ["HERMES_HOME"]) / "desktop-build-stamp.json").exists()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("failure", ["pnpm-unavailable", "desktop-after-long-feature-failure"])
def test_unbuilt_desktop_is_named_on_one_whole_line(source_products, monkeypatch, capsys, failure):
    # The Desktop hand-off keys on this line: the receipt follow-up is truncated and leads with
    # whichever product failed first, and a pnpm acquisition failure used to escape un-named.
    import hermes_cli.main_install_repair as install_repair
    from hermes_cli.source_build import ProductBuildError, build_update_products

    root, _ = source_products

    def fail(error):
        def raiser(*_args, **_kwargs):
            raise error
        return raiser

    if failure == "pnpm-unavailable":
        monkeypatch.setattr(pm, "ensure", fail(pm.InstallError("pnpm", "unavailable")))
        expected = "Desktop app build owed: Node dependencies failed"
    else:
        monkeypatch.setattr(install_repair, "_install_configured_features_missing_deps",
                            fail(RuntimeError("pip install failed: " + "x" * 600)))
        (root / "fail-desktop").touch()
        expected = "Desktop app build owed: desktop app build failed"
    with pytest.raises(ProductBuildError):
        build_update_products(root, desktop=True)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert [line for line in lines if line.startswith("Desktop app build owed:")] == [expected]


@pytest.mark.platforms("linux")
def test_desktop_dependency_failure_still_builds_the_tui_and_web_ui(source_products, capsys):
    # A host whose compiler rejects a Desktop-only native addon (node-pty on g++ 9) used to lose
    # every frontend with it: the union install failed and nothing was built.
    from hermes_cli.source_build import ProductBuildError, build_update_products

    root, _ = source_products
    manifest = root / "apps/desktop/package.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "scripts": {"postinstall": "exit 1"}}))
    with pytest.raises(ProductBuildError) as failure:
        build_update_products(root, desktop=True)
    assert [name for name, _ in failure.value.failures] == ["desktop dependencies"]
    # pnpm may run the root's postinstall before the desktop workspace's fails; only products count.
    assert [event["step"] for event in _events(root) if event["step"] != "deps"] == ["tui", "web"]
    assert not (root / "apps/desktop/node_modules").exists()
    out = capsys.readouterr().out
    assert "Desktop app build owed: desktop dependencies failed" in out
    assert "hermes uninstall --gui" in out


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("desktop", [False, True])
def test_module_cli_builds_the_requested_products(source_products, desktop, monkeypatch):
    import runpy

    root, acquired = source_products
    monkeypatch.setattr(sys, "argv", ["source_build", "--source", str(root)] + (["--desktop"] if desktop else []))
    # run_module exercises __main__ while substituting only tool acquisition.
    monkeypatch.delitem(sys.modules, "hermes_cli.source_build", raising=False)
    runpy.run_module("hermes_cli.source_build", run_name="__main__")
    assert acquired == ["pnpm"]
    assert (root / "hermes_cli/web_dist/index.html").is_file()
    assert (root / "apps/desktop/release/linux-unpacked/hermes").exists() == desktop


@pytest.mark.platforms("macos")
def test_packaged_desktop_is_reused_only_while_it_names_head(tmp_path, monkeypatch):
    """The update skips the desktop build only when the shipped app's baked commit is HEAD, or
    [daveey] an ancestor of HEAD that no desktop input changed since, and its receipt is current.
    A desktop input change, an unrelated commit or an unreadable stamp means build."""
    from hermes_cli import main_desktop

    root = tmp_path / "checkout"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    (root / ".git/info/exclude").write_text("apps/desktop/release/\n", encoding="utf-8")  # as in the repo
    desktop_dir = root / "apps/desktop"
    exe = desktop_dir / "release/app/Hermes"
    monkeypatch.setattr(main_desktop, "_desktop_packaged_executable", lambda _d: exe)
    resources = main_desktop._packaged_resources_dir(desktop_dir)
    (resources / "app.asar.unpacked/dist").mkdir(parents=True)
    receipt_current = {"value": True}
    monkeypatch.setattr(main_desktop, "_desktop_build_needed",
                        lambda *_a, **_k: not receipt_current["value"])

    def head():
        return subprocess.run([*git[:3], "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def commit_file(rel):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel, encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", rel], check=True)

    def reused():
        return main_desktop._packaged_desktop_current_for_head(desktop_dir, root)

    assert not reused()  # no baked stamp
    (resources / "install-stamp.json").write_text(json.dumps({"commit": head()}), encoding="utf-8")
    assert reused()
    receipt_current["value"] = False
    assert not reused()
    receipt_current["value"] = True
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "two"], check=True)
    commit_file("agent/run.py")
    assert reused()  # HEAD moved, but nothing the desktop build reads changed
    commit_file("apps/desktop/src/a.ts")
    assert not reused()  # a desktop input changed after the baked commit
    (resources / "install-stamp.json").write_text(json.dumps({"commit": head()}), encoding="utf-8")
    assert reused()
    subprocess.run([*git, "checkout", "-q", "--orphan", "other"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "unrelated"], check=True)
    assert not reused()  # the baked commit is not an ancestor of HEAD


def test_desktop_input_paths_mirror_the_desktop_source_hash():
    """The git-level check covers exactly the inputs sourceHash('desktop') hashes."""
    import re

    from hermes_cli import main_desktop

    src = (Path(__file__).resolve().parents[2] / "scripts/build/freshness.mjs").read_text(encoding="utf-8")
    # productInputs() lists the inputs sourceHash(desktop) hashes after its workspace entry.
    body = src[src.index("function productInputs"):]
    listed = body[body.index("workspace,") + len("workspace,"):body.index("]\n}")]
    listed = re.sub(r"//[^\n]*", "", listed)
    paths = ["apps/desktop", *re.findall(r"'([^']+)'", listed)]
    assert sorted(main_desktop.DESKTOP_SOURCE_INPUT_PATHS) == sorted(paths)


def _prebuilt_fixture(tmp_path, monkeypatch, *, stamp_commit=None, head_change=None):
    """A checkout at HEAD with a live packaged app, plus a prebuilt app staged elsewhere
    (HERMES_DESKTOP_PREBUILT_APP). Records whether the normal desktop build ran."""
    from hermes_cli import main_desktop, source_build

    monkeypatch.setattr(sys, "platform", "darwin")
    root = tmp_path / "checkout"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    head = subprocess.run([*git[:3], "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    if head_change:
        # The prebuilt names commit "one"; HEAD moves past it with one change at ``head_change``.
        stamp_commit = stamp_commit or head
        (root / head_change).parent.mkdir(parents=True, exist_ok=True)
        (root / head_change).write_text("changed", encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", head_change], check=True)

    def app_at(base, marker, commit):
        app = base / "mac-arm64/Hermes.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/Hermes").write_text(marker)
        dist = app / "Contents/Resources/app.asar.unpacked/dist"
        dist.mkdir(parents=True)
        (app / "Contents/Resources/install-stamp.json").write_text(json.dumps({"commit": commit}))
        return app

    desktop_dir = root / "apps/desktop"
    app_at(desktop_dir / "release", "previous app", "0" * 40)
    prebuilt = app_at(tmp_path / "stage-release", "prebuilt app", stamp_commit or head)
    monkeypatch.setenv("HERMES_DESKTOP_PREBUILT_APP", str(prebuilt))

    # Receipt/signature/bundle-shape checks are covered by their own tests; here they pass.
    monkeypatch.setattr(source_build, "source_product_current", lambda *_a, **_k: True)
    monkeypatch.setattr(main_desktop, "_renderer_bundle_torn", lambda _d: False)
    monkeypatch.setattr(main_desktop, "_packaged_node_pty_missing", lambda _d: False)
    monkeypatch.setattr(main_desktop, "_prebuilt_signature_error", lambda _app: None, raising=False)
    monkeypatch.setattr(main_desktop, "_stage_macos_bundle_copy",
                        lambda src, dst: shutil.copytree(src, dst, symlinks=True))
    monkeypatch.setattr(main_desktop, "_refresh_installed_desktop_apps", lambda _d: None)
    monkeypatch.setattr(main_desktop, "_packaged_desktop_current_for_head", lambda *_a: False)
    built = []
    monkeypatch.setattr(main_desktop, "build_prepared_desktop", lambda *a, **k: built.append(a))
    return root, desktop_dir, built


@pytest.mark.platforms("macos")
def test_update_swaps_in_a_prebuilt_desktop_app_that_names_head(tmp_path, monkeypatch, capsys):
    """HERMES_DESKTOP_PREBUILT_APP built from HEAD (hermes-sync's background stage) is swapped in
    through the stage-and-swap path instead of running npm."""
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch)
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert built == []
    live = desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
    assert live.read_text() == "prebuilt app"
    assert not list(desktop_dir.glob(".staging-*"))
    assert not (desktop_dir / "release/mac-arm64.previous").exists()
    # The prebuilt stays where it was: a failed apply can retry with it.
    assert Path(os.environ["HERMES_DESKTOP_PREBUILT_APP"], "Contents/MacOS/Hermes").read_text() == "prebuilt app"
    assert "prebuilt desktop app" in capsys.readouterr().out.lower()


@pytest.mark.platforms("macos")
def test_update_builds_when_the_prebuilt_desktop_app_names_another_commit(tmp_path, monkeypatch, capsys):
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch, stamp_commit="f" * 40)
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert len(built) == 1
    live = desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
    assert live.read_text() == "previous app"
    assert "not used" in capsys.readouterr().out


@pytest.mark.platforms("macos")
def test_update_builds_when_the_prebuilt_desktop_app_fails_its_signature(tmp_path, monkeypatch):
    from hermes_cli import main_desktop
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(main_desktop, "_prebuilt_signature_error", lambda _app: "signature does not verify")
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert len(built) == 1
    assert (desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes").read_text() == "previous app"


@pytest.mark.platforms("macos")
def test_update_builds_without_a_prebuilt_desktop_app(tmp_path, monkeypatch):
    from hermes_cli.source_build import _build_desktop_product

    root, _desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_DESKTOP_PREBUILT_APP")
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert len(built) == 1


@pytest.mark.platforms("macos")
def test_update_swaps_in_a_prebuilt_from_an_ancestor_with_unchanged_desktop_inputs(tmp_path, monkeypatch, capsys):
    """[daveey] hermes-sync keeps one build while new heads touch no desktop input. A prebuilt
    stamped with such an ancestor of HEAD is installed, and nothing afterwards rebuilds it: the
    re-entered update reuses it and `hermes desktop` launch reads it as current."""
    from hermes_cli import main_desktop
    from hermes_cli.source_build import _build_desktop_product

    real_head_gate = main_desktop._packaged_desktop_current_for_head
    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch, head_change="agent/run.py")
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert built == []
    live = desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
    assert live.read_text() == "prebuilt app"
    assert "installed the prebuilt desktop app" in capsys.readouterr().out.lower()

    # After install: the real HEAD gate (an "Already up to date" re-entry, a retried tail) and
    # the launch freshness gate both keep the installed app.
    monkeypatch.setattr(main_desktop, "_packaged_desktop_current_for_head", real_head_gate)
    assert main_desktop._packaged_desktop_current_for_head(desktop_dir, root)
    assert not main_desktop._desktop_build_needed(desktop_dir, root, source_mode=False)
    monkeypatch.delenv("HERMES_DESKTOP_PREBUILT_APP")
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert built == []
    assert live.read_text() == "prebuilt app"


@pytest.mark.platforms("macos")
def test_defer_desktop_swap_leaves_a_running_desktop_alone(tmp_path, monkeypatch, capsys):
    """[daveey] HERMES_UPDATE_DEFER_DESKTOP_SWAP=1 (hermes-sync, while Desktop is open): the core update
    lands but the bundle is neither built nor swapped, so the running Desktop is never stopped
    (t_01a1825a82e472f6). hermes-sync swaps it on the next quit or while the user is away."""
    from hermes_cli import main_desktop
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_UPDATE_DEFER_DESKTOP_SWAP", "1")
    monkeypatch.setattr(main_desktop, "_desktop_main_launches", lambda _d: {4242: []})
    stopped = []
    monkeypatch.setattr(main_desktop, "_stop_desktop_processes_locking_build",
                        lambda *a, **k: stopped.append(a) or [4242])
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert built == [] and stopped == []
    assert (desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes").read_text() == "previous app"
    assert "desktop app swap deferred" in capsys.readouterr().out.lower()


@pytest.mark.platforms("macos")
def test_defer_desktop_swap_still_installs_when_desktop_is_not_running(tmp_path, monkeypatch):
    """With the flag but no Desktop main process (it quit), the swap runs as usual."""
    from hermes_cli import main_desktop
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, _built = _prebuilt_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_UPDATE_DEFER_DESKTOP_SWAP", "1")
    monkeypatch.setattr(main_desktop, "_desktop_main_launches", lambda _d: {})
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert (desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes").read_text() == "prebuilt app"


@pytest.mark.platforms("macos")
def test_without_the_defer_flag_a_running_desktop_is_swapped_as_upstream(tmp_path, monkeypatch):
    from hermes_cli import main_desktop
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, _built = _prebuilt_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_UPDATE_DEFER_DESKTOP_SWAP", raising=False)
    monkeypatch.setattr(main_desktop, "_desktop_main_launches", lambda _d: {4242: []})
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert (desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes").read_text() == "prebuilt app"


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("change", ["apps/desktop/src/a.ts", "apps/shared/src/x.ts", "pnpm-lock.yaml", "pnpm-workspace.yaml"])
def test_update_builds_when_a_desktop_input_changed_since_the_prebuilt_commit(tmp_path, monkeypatch, capsys, change):
    from hermes_cli.source_build import _build_desktop_product

    root, desktop_dir, built = _prebuilt_fixture(tmp_path, monkeypatch, head_change=change)
    _build_desktop_product(root, {"PATH": os.environ["PATH"]}, lambda _label: None)
    assert len(built) == 1
    assert (desktop_dir / "release/mac-arm64/Hermes.app/Contents/MacOS/Hermes").read_text() == "previous app"
    assert "not used" in capsys.readouterr().out


def _record_packaged_desktop(source, app, *, builder=None):
    """[daveey] Pack-shaped desktop product: ``builder`` (a checkout with prepared inputs) compiles
    into ``app``'s Resources/app.asar.unpacked/dist and records its receipt there, and the bundle
    carries the baked stamp as Resources/install-stamp.json (electron-builder extraResources)."""
    builder = builder or source
    build = builder / "apps/desktop/build"
    prepared = {"icons": str(builder / "apps/desktop/public"), "nativeDeps": str(build / "native-deps"),
                "stamp": str(build / "install-stamp.json")}
    dist = app / "Contents/Resources/app.asar.unpacked/dist"
    dist.mkdir(parents=True)
    (dist / "main.js").write_text("main", encoding="utf-8")
    clock = json.loads(Path(prepared["stamp"]).read_text(encoding="utf-8"))["builtAt"]
    script = (builder / "scripts/build/freshness.mjs").as_uri()
    subprocess.run([shutil.which("node"), "--input-type=module", "-e",
                    f"import {{recordProduct, buildInputs}} from {json.dumps(script)};"
                    "const [source, out, prepared, stampClock] = process.argv.slice(1);"
                    "recordProduct({source, product: 'desktop', out,"
                    " inputs: buildInputs(source, 'desktop', JSON.parse(prepared)), stampClock});",
                    str(builder), str(dist), json.dumps(prepared), clock], check=True)
    shutil.copy2(prepared["stamp"], app / "Contents/Resources/install-stamp.json")
    return dist


def _desktop_checkout(root, commit, clock):
    copy_freshness_scripts(root)
    for rel, text in {"apps/desktop/src/a.ts": "a", "apps/desktop/public/icon.png": "icon",
                      "apps/desktop/build/native-deps/pty.node": "pty", "package.json": "{}"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _write_stamp(root / "apps/desktop/build/install-stamp.json", commit, clock)
    return root


def _write_stamp(path, commit, clock):
    path.write_text(json.dumps({"schemaVersion": 1, "commit": commit, "builtAt": clock}), encoding="utf-8")


def test_installed_prebuilt_desktop_stays_current_after_the_stage_moves(tmp_path, monkeypatch):
    """[daveey] hermes-sync packs the next origin/main in its stage worktree, so a prebuilt app's
    receipt names prepared inputs (icons, native deps, the baked stamp) in that stage. Once the
    app is installed in the live checkout, the stage moving on to another commit must not make it
    read stale: the bundle's own Resources/install-stamp.json is the stamp it shipped. A changed
    desktop input in the live checkout, or a bundle stamp whose clock is not the baked one
    (#123308), still reads stale."""
    from hermes_cli.source_build import source_product_current

    use_host_node_as_pm_node(monkeypatch)
    live = _desktop_checkout(tmp_path / "live", "a" * 40, "2026-01-01T00:00:00.000Z")
    stage = _desktop_checkout(tmp_path / "stage", "b" * 40, "2026-01-02T00:00:00.000Z")
    app = live / "apps/desktop/release/mac-arm64/Hermes.app"
    dist = _record_packaged_desktop(live, app, builder=stage)

    def current():
        return source_product_current(live, "desktop", dist)

    assert current()
    # The stage checks out and stamps a newer commit, regenerating its prepared inputs.
    _write_stamp(stage / "apps/desktop/build/install-stamp.json", "c" * 40, "2026-01-03T00:00:00.000Z")
    (stage / "apps/desktop/public/icon.png").write_text("new icon", encoding="utf-8")
    (stage / "apps/desktop/build/native-deps/pty.node").write_text("new pty", encoding="utf-8")
    assert current()
    shutil.rmtree(stage)  # pruned altogether
    assert current()

    # The bundle's shipped stamp still has to be the one the build baked.
    shipped = app / "Contents/Resources/install-stamp.json"
    original = shipped.read_bytes()
    _write_stamp(shipped, "b" * 40, "2026-01-09T00:00:00.000Z")
    assert not current()
    _write_stamp(shipped, "d" * 40, "2026-01-02T00:00:00.000Z")
    assert not current()  # same clock, different provenance
    shipped.unlink()
    assert not current()
    shipped.write_bytes(original)
    assert current()

    # A desktop input that changed in the live checkout still means rebuild.
    (live / "apps/desktop/src/a.ts").write_text("changed", encoding="utf-8")
    assert not current()


def test_in_place_packaged_desktop_still_tracks_its_own_prepared_inputs(tmp_path, monkeypatch):
    """[daveey] Control: an app packed in the live checkout itself keeps re-hashing that checkout's
    prepared inputs, as before."""
    from hermes_cli.source_build import source_product_current

    use_host_node_as_pm_node(monkeypatch)
    live = _desktop_checkout(tmp_path / "live", "a" * 40, "2026-01-01T00:00:00.000Z")
    dist = _record_packaged_desktop(live, live / "apps/desktop/release/mac-arm64/Hermes.app")
    assert source_product_current(live, "desktop", dist)
    (live / "apps/desktop/build/native-deps/pty.node").write_text("new pty", encoding="utf-8")
    assert not source_product_current(live, "desktop", dist)


@pytest.mark.platforms("linux", "macos")
def test_product_freshness_reads_through_a_symlinked_checkout(tmp_path, monkeypatch):
    """[daveey] freshness.mjs only runs its CLI when argv[1] is its real path, so a checkout reached
    through a symlink (/var vs /private/var) used to read as never current."""
    from hermes_cli.source_build import source_product_current

    use_host_node_as_pm_node(monkeypatch)
    live = _desktop_checkout(tmp_path / "live", "a" * 40, "2026-01-01T00:00:00.000Z")
    dist = _record_packaged_desktop(live, live / "apps/desktop/release/mac-arm64/Hermes.app")
    link = tmp_path / "link"
    link.symlink_to(live, target_is_directory=True)
    assert source_product_current(link, "desktop", link / dist.relative_to(live))


@pytest.mark.parametrize("enabled", [False, True])
def test_update_rebuilds_the_webapp_bundle_a_home_serves(tmp_path, monkeypatch, enabled):
    """``hermes update`` rebuilds ``apps/desktop/dist-webapp``, with the function ``hermes webapp``
    uses, when any home sharing this install has ``webapp.enabled``: that home's gateway serves the
    bundle as built, so an old one would talk to the updated backend. Otherwise it is left alone."""
    import hermes_cli.left_core_migration as left_core
    import hermes_cli.main_install_repair as install_repair
    import hermes_cli.memory_provider_migration as memory_migration
    from hermes_cli import update_stage, webapp
    from hermes_cli.source_build import build_update_products
    from pm import plugins_state

    root = tmp_path / "checkout"
    (root / "apps/desktop/scripts").mkdir(parents=True)
    (root / "apps/desktop/scripts/build-webapp.mjs").write_text("", encoding="utf-8")
    homes = [tmp_path / "default", tmp_path / "profiles" / "work"]
    for home in homes:
        home.mkdir(parents=True)
    (homes[1] / "config.yaml").write_text(f"webapp:\n  enabled: {str(enabled).lower()}\n", encoding="utf-8")
    builds = []
    monkeypatch.setattr(install_repair, "_install_configured_features_missing_deps", lambda _root: None)
    monkeypatch.setattr(memory_migration, "migrate_all_homes", list)
    monkeypatch.setattr(left_core, "migrate_all_homes", list)
    monkeypatch.setattr(update_stage, "publish_stage", lambda _stage: None)
    monkeypatch.setattr(plugins_state, "dependency_homes", lambda: homes)
    monkeypatch.setattr(webapp, "prepare_webapp_renderer",
                        lambda project_root, **options: builds.append((project_root, options)))

    build_update_products(root, desktop=False)

    assert builds == ([(root, {"explicit": True})] if enabled else [])


@pytest.mark.parametrize("built", [True, False])
def test_a_failed_webapp_rebuild_in_update_marks_the_served_bundle_out_of_date(tmp_path, monkeypatch, capsys, built):
    """When ``hermes update`` cannot rebuild the Webapp bundle a home serves, the previous bundle
    stays on disk but is marked out of date: its gateway reports the Webapp unavailable and says to
    run ``hermes webapp`` (which rebuilds it) instead of serving it against the updated backend. The
    update still reports the failure. With no bundle built there is nothing to mark."""
    import hermes_cli.left_core_migration as left_core
    import hermes_cli.main_install_repair as install_repair
    import hermes_cli.memory_provider_migration as memory_migration
    from gateway.webapp_listener import NOT_BUILT, bundle_missing_reason
    from hermes_cli import update_stage, webapp
    from hermes_cli.source_build import ProductBuildError, build_update_products
    from pm import plugins_state

    root = tmp_path / "checkout"
    (root / "apps/desktop/scripts").mkdir(parents=True)
    (root / "apps/desktop/scripts/build-webapp.mjs").write_text("", encoding="utf-8")
    dist = webapp.webapp_dist_dir(root)
    dist.mkdir()
    if built:
        (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    home = tmp_path / "default"
    home.mkdir()
    (home / "config.yaml").write_text("webapp:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(install_repair, "_install_configured_features_missing_deps", lambda _root: None)
    monkeypatch.setattr(memory_migration, "migrate_all_homes", list)
    monkeypatch.setattr(left_core, "migrate_all_homes", list)
    monkeypatch.setattr(update_stage, "publish_stage", lambda _stage: None)
    monkeypatch.setattr(plugins_state, "dependency_homes", lambda: [home])

    def fail(_project_root, **_options):
        raise webapp.WebappBuildError("Browser-hosted Desktop build failed: npm ci exited 1")

    monkeypatch.setattr(webapp, "prepare_webapp_renderer", fail)
    before = sorted(root.rglob("*"))

    with pytest.raises(ProductBuildError, match="Webapp build"):
        build_update_products(root, desktop=False)

    reason = bundle_missing_reason(dist)
    assert reason is not None and reason.startswith(NOT_BUILT) and "hermes webapp" in reason
    if built:
        assert (dist / "index.html").read_text(encoding="utf-8") == "<!doctype html>"
        assert "hermes webapp" in capsys.readouterr().out
    else:
        assert sorted(root.rglob("*")) == before
