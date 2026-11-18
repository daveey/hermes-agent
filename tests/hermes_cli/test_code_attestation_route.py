"""Route-aware bootstrap attestation (R3-A..R3-G): REAL subprocesses via the real stub shape.

Positive: the managed stub (current template, and the backed-up Redde stub with only its root
rewritten) runs ``dashboard`` through the real hermes_bootstrap and the real hermes_cli.main
parser; only the final ``cmd_dashboard`` dispatch is intercepted (a committed fixture-tree edit
of that one function entry, so main still equals its boot blob). Negatives refuse.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

# Every run is a throwaway tmp repo; the stub text itself trips the `hermes update` guard.
pytestmark = pytest.mark.live_system_guard_bypass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
DATA = Path(__file__).resolve().parent / "fixtures"
BACKUP_STUB = DATA / "hermes_redde_stub_2026-10-22.sh"
BACKUP_ROOT = "/Users/daveey/code/hermes"
SKIP_DIRS = ("tests/", "website/", "evals/", "scripts/", "optional-skills/", "skills/",
             "ui-tui/", "nix/", "apps/", "hosts/", ".github/", ".hermes/")
DATA_TOP = {"hermes_cli", "pm", "agent", "gateway", "tools", "cron", "hermes_platform",
            "tui_gateway", "acp_adapter", "plugins", "providers", "hermes_agent"}
PROBE_MODULE = '''import json, os, sys
from hermes_cli import code_attestation as ca
ledger = ca._ledger()
json.dump({
    "report": ledger.route_report(), "seed": ledger.trusted_bootstrap_seed(),
    "unknowns": list(ca.unknowns()), "units": ca.executed_units(),
    "deps": ca.dependency_receipts(),
    "modules": sorted(m for m in sys.modules if m.startswith("hermes_cli")),
    "argv": sys.argv,
}, open(os.environ["ATTEST_PROBE_OUT"], "w"), default=str)
'''
DASHBOARD_PROBE = '''

def _attest_probe_exit(args):
    # Test-only interception of the FINAL cmd_dashboard dispatch: report, do not serve.
    import attest_probe  # noqa: F401  (dumps the ledger at import)
    return 0
'''
BOOTSTRAP_PROBE_EXTRA = "FORBIDDEN_PREFIXES = ()\n"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", *args],
        check=True, capture_output=True, text=True, env={"PATH": os.defpath, "HOME": str(root)}).stdout


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    """A committed copy of the working tree (disk == boot blob), dashboard entry intercepted."""
    dest = tmp_path_factory.mktemp("core") / "core"
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, check=True).stdout.split(b"\0")
    for raw in filter(None, listing):
        name = os.fsdecode(raw)
        source = ROOT / name
        if name.startswith(SKIP_DIRS) or not source.is_file():
            continue
        top = name.split("/", 1)[0]
        if not (name.endswith(".py") or (top in DATA_TOP and source.stat().st_size < 262144)):
            continue
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / name)
    main = dest / "hermes_cli" / "main.py"
    text = main.read_text()
    assert text.count("def cmd_dashboard(args):\n") == 1
    main.write_text(text.replace(
        "def cmd_dashboard(args):\n",
        DASHBOARD_PROBE + "\n\ndef cmd_dashboard(args):\n    return _attest_probe_exit(args)\n\n\n"
        "def _real_cmd_dashboard(args):\n", 1))
    handoff = dest / "hermes_cli" / "update_handoff.py"
    text = handoff.read_text()
    marker = "def _continue_legacy_post_swap(handoff_path: str | Path, *, argv_tail: list[str]) -> int:\n"
    assert text.count(marker) == 1
    handoff.write_text(text.replace(marker, marker + "    import attest_probe  # noqa: F401\n    return 0\n", 1))
    (dest / "attest_probe.py").write_text(PROBE_MODULE)
    _git(dest, "init", "-q")
    # A commit may detach maintenance that repacks/removes loose objects while
    # consumers copy this repository. Persist the policy so copies inherit it;
    # _git intentionally drops the runner's GIT_CONFIG_* environment settings.
    _git(dest, "config", "maintenance.auto", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "fixture")
    return dest


def make_python(directory, pth=None, modules=None):
    """A venv interpreter that reaches the test environment's packages through a plain path-only
    .pth (no editable finder is preloaded), optionally with an extra pre-hook ``.pth`` import."""
    subprocess.run([sys._base_executable, "-m", "venv", "--without-pip", str(directory)], check=True,
                   capture_output=True)
    site = next((directory / "lib").glob("python*/site-packages"))
    (site / "deps.pth").write_text("\n".join(p for p in sys.path if "site-packages" in p) + "\n")
    for name, text in (modules or {}).items():
        (site / name).write_text(text)
    for name, text in (pth or {}).items():
        (site / name).write_text(text)
    return directory / "bin" / "python"


@pytest.fixture(scope="module")
def python():
    # The supported target's shape: an interpreter whose dependency site-packages is an
    # installer-built tree with RECORD receipts (here the test environment's own venv).
    # make_python() venvs (path-only deps.pth, extra pre-hook files) are refusal fixtures.
    return Path(sys.executable)


def current_script(root):
    from hermes_cli import _launch_template
    return _launch_template.render(str(root), "hermes_cli.main", "main")


def backup_script(root):
    script = shlex.split(BACKUP_STUB.read_text().split("\n", 1)[1])[4]
    assert repr(BACKUP_ROOT) in script
    return script.replace(repr(BACKUP_ROOT), repr(str(root)))


ARGS = ("dashboard", "--host", "127.0.0.1", "--skip-build", "--no-open")


def run(tmp_path, command, extra_env=None, cwd=None):
    out = tmp_path / "probe.json"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "HERMES_HOME": str(home),
           "ATTEST_PROBE_OUT": str(out), "LANG": "C.UTF-8", **(extra_env or {})}
    result = subprocess.run(command, cwd=cwd or tmp_path, capture_output=True, text=True, env=env,
                            timeout=180, check=False)
    probe = json.loads(out.read_text()) if out.exists() else None
    assert probe is not None, (result.returncode, result.stdout[-1500:], result.stderr[-2500:])
    return probe


def run_stub(tmp_path, python, script, args=ARGS, extra_env=None):
    return run(tmp_path, [str(python), "-I", "-c", script, *args], extra_env)


def blob(root, name):
    return _git(root, "rev-parse", "HEAD:" + name).strip()


def refused(probe, reason):
    assert any(u == reason or u.startswith(reason) for u in probe["unknowns"]), probe["unknowns"]
    assert "bootstrap-provenance-pending" in probe["unknowns"]
    assert probe["report"]["route"] is None and probe["report"]["launcher"] is None


def assert_supported(probe, core, digest, route="managed-stub"):
    from hermes_cli.code_attestation import SEED_COMMON
    report = probe["report"]
    assert report["route"] == route and report["stub_head_digest"] == digest
    assert report["main_is_seed"] is False
    seeds = {s["module"]: s for s in probe["seed"] if s["kind"] == "trusted_bootstrap_seed"}
    assert set(seeds) == set(SEED_COMMON) and "hermes_cli.main" not in seeds
    for module, relative in SEED_COMMON.items():
        assert seeds[module]["git_blob"] == blob(core, relative)
    launchers = [s for s in probe["seed"] if s["kind"] == "trusted_launcher_script"]
    assert len(launchers) == 1 and launchers[0]["head_sha256"] == digest
    assert "bootstrap-provenance-pending" not in probe["unknowns"]
    # R4-B: the supported route has NO residual unknowns (no dependency allowlist).
    assert probe["unknowns"] == [], probe["unknowns"]
    deps = probe["deps"]
    assert deps["trust"] == "installer-receipt: trusted, not audited, no coverage"
    assert deps["dists"] and all(u["component"].startswith("dependency:") for u in deps["units"])
    # Dependency receipts never appear among audited executed units (R4-H).
    assert not [u for u in probe["units"] if u.get("component", "").startswith("dependency:")]
    assert not [u for u in probe["units"] if "site-packages" in u.get("filename", "")]
    units = {Path(u["filename"]).relative_to(core).as_posix(): u for u in probe["units"]
             if u.get("filename", "").startswith(str(core))}
    main_text = (core / "hermes_cli/main.py").read_bytes()
    assert units["hermes_cli/main.py"]["source_sha256"] == hashlib.sha256(main_text).hexdigest()
    assert {"pm/environments.py", "hermes_cli/_parser.py", "hermes_cli/venv_sync.py"} <= set(units)
    assert "hermes_cli.main" in probe["modules"]
    # Seed modules were never retroactively audited: none appears as an executed unit.
    assert not {"hermes_bootstrap.py", "hermes_cli/local_work.py", "hermes_cli/code_attestation.py",
                "hermes_cli/_launch_template.py", "hermes_cli/_early_recovery.py"} & set(units)


def test_managed_stub_dashboard_current_template(tmp_path, core, python):
    from hermes_cli import code_attestation, _launch_template
    probe = run_stub(tmp_path, python, current_script(core))
    assert_supported(probe, core, code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST)
    script = current_script(core)
    head, _ = _launch_template.split_pin_marker(script)
    assert hashlib.sha256(_launch_template.normalized_head(head, str(core)).encode()).hexdigest() \
        == code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST


def test_managed_stub_dashboard_backed_up_redde_stub_verbatim(tmp_path, core, python):
    from hermes_cli import code_attestation
    probe = run_stub(tmp_path, python, backup_script(core))
    assert_supported(probe, core, code_attestation.BACKUP_STUB_HEAD_DIGEST)
    assert code_attestation.BACKUP_STUB_HEAD_DIGEST != code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST


def test_serve_alias_reaches_the_same_route(tmp_path, core, python):
    from hermes_cli import code_attestation
    probe = run_stub(tmp_path, python, current_script(core), args=("serve",))
    assert_supported(probe, core, code_attestation.CURRENT_TEMPLATE_HEAD_DIGEST)


def test_console_route_main_first_is_refused_not_guessed(tmp_path, core, python):
    stub = tmp_path / "console_stub.py"
    stub.write_text(f"import sys\nsys.path.insert(0, {str(core)!r})\nfrom hermes_cli.main import main\n"
                    "sys.exit(main())\n")
    probe = run(tmp_path, [str(python), "-I", str(stub), *ARGS])
    refused(probe, "unsupported-route:console-main-first")


def test_modified_tail_line_refuses(tmp_path, core, python):
    script = current_script(core)
    changed = script.replace("sys.exit(main())\n", "sys.exit(main() or 0)\n")
    assert changed != script
    refused(run_stub(tmp_path, python, changed), "unsupported-route:stub-tail")


def test_head_digest_outside_reviewed_set_refuses(tmp_path, core, python):
    script = current_script(core)
    changed = script.replace("os.environ.pop('PYTHONHOME', None)\n",
                             "os.environ.pop('PYTHONHOME', None)\npass\n", 1)
    assert changed != script
    refused(run_stub(tmp_path, python, changed), "unsupported-stub-head")


def test_stub_for_another_root_refuses(tmp_path, core, python):
    # The stub names an alias path (a symlink to the same checkout): bootstrap still
    # imports, but the root literal is not the resolved core root, so it refuses.
    from hermes_cli import _launch_template
    alias = tmp_path / "alias-root"
    alias.symlink_to(core, target_is_directory=True)
    script = _launch_template.render(str(alias), "hermes_cli.main", "main")
    refused(run_stub(tmp_path, python, script), "unsupported-stub-root")


def test_extra_prehook_pth_plugin_refuses(tmp_path, core):
    interpreter = make_python(tmp_path / "v", pth={"evil.pth": "import attest_evil\n"},
                              modules={"attest_evil.py": "VALUE = 1\n"})
    probe = run_stub(tmp_path, interpreter, current_script(core))
    assert "prehook-unrooted:attest_evil" in probe["unknowns"]
    assert "bootstrap-provenance-pending" in probe["unknowns"]


def test_main_already_imported_before_hook_refuses(tmp_path, core):
    evil = (f"import sys\nsys.path.insert(0, {str(core)!r})\n"
            "import hermes_cli.main\n")
    interpreter = make_python(tmp_path / "v", pth={"evil.pth": "import attest_main_first\n"},
                              modules={"attest_main_first.py": evil})
    probe = run_stub(tmp_path, interpreter, current_script(core))
    refused(probe, "unexpected-prehook:hermes_cli.main")


def test_run_module_argv_refuses(tmp_path, core, python):
    probe = run_stub(tmp_path, python, current_script(core), args=("--run-module", "attest_probe"))
    refused(probe, "unsupported-route:run-module")


def test_post_swap_refuses(tmp_path, core, python):
    probe = run_stub(tmp_path, python, current_script(core), args=("update", "--post-swap", str(tmp_path / "x")))
    refused(probe, "unsupported-route:post-swap")


def test_runtime_command_c_form_refuses(tmp_path, core, python):
    from hermes_cli import _launchers
    command = _launchers.runtime_command(core, ARGS, python=python)
    assert command[1:3] == ["-I", "-c"]
    probe = run(tmp_path, command)
    refused(probe, "unsupported-route:")


def test_real_script_only_as_comment_or_string_refuses(tmp_path, core, python):
    script = current_script(core)
    comment = "\n".join("# " + line for line in script.split("\n")) + (
        f"\nimport sys; sys.path.insert(0, {str(core)!r})\nimport hermes_bootstrap\n"
        "from hermes_cli.main import main\nsys.exit(main())\n")
    probe = run_stub(tmp_path, python, comment)
    assert any(u.startswith("unsupported-route:") for u in probe["unknowns"]), probe["unknowns"]
    literal = (f"import sys; sys.path.insert(0, {str(core)!r}); TEXT = {script!r}\n"
               "import hermes_bootstrap\nfrom hermes_cli.main import main\nsys.exit(main())\n")
    probe = run_stub(tmp_path / "second" if (tmp_path / "second").mkdir() is None else tmp_path, python, literal)
    refused(probe, "unsupported-route:no-pin-marker")


def test_hand_set_pin_flag_on_non_stub_launch_refuses(tmp_path, core, python):
    script = (f"import sys\nsys.path.insert(0, {str(core)!r})\nsys._hermes_pin_default_home = True\n"
              "try:\n    import hermes_bootstrap\nexcept Exception:\n    raise\n"
              "from hermes_cli.main import main\nsys.exit(main())\n")
    probe = run_stub(tmp_path, python, script)
    refused(probe, "unsupported-route:stub-tail")
    assert probe["report"]["stub_head_digest"] is None


def test_dirty_core_against_boot_blob_is_unknown(tmp_path, core, python):
    dirty = tmp_path / "dirty"
    shutil.copytree(core, dirty, symlinks=True)
    target = dirty / "hermes_cli" / "_early_recovery.py"
    target.write_text(target.read_text() + "\n# dirty after boot commit\n")
    probe = run_stub(tmp_path, python, current_script(dirty))
    assert "seed-source-skew:hermes_cli._early_recovery" in probe["unknowns"]
    assert "bootstrap-provenance-pending" in probe["unknowns"]
