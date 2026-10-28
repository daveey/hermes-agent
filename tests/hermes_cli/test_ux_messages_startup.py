"""Invariant tests for the plain-language startup messages of the ``hermes`` CLI.

Contracts (not snapshots): a mistyped subcommand names the typo, offers the closest
match and ``hermes --help`` without dumping the 70-entry choice list; a rejected
profile name explains the rule; missing optional dependencies point at ``hermes update``.
"""

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from hermes_cli._parser import build_top_level_parser

def _parse_error(argv: list[str]) -> str:
    parser, subparsers, _chat = build_top_level_parser()
    for name in ("sessions", "model", "profile"):
        subparsers.add_parser(name)
    gw = subparsers.add_parser("gateway").add_subparsers(dest="gateway_action", metavar="<action>")
    for name in ("start", "stop", "status"):
        gw.add_parser(name)
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2
    return err.getvalue()

def test_unknown_subcommand_names_typo_suggests_closest_and_hides_choice_list():
    text = _parse_error(["sesions"])
    assert "'sesions' is not a `hermes` command" in text
    assert "Did you mean: sessions" in text
    assert "hermes --help" in text
    assert "choose from" not in text
    assert "invalid choice" not in text

def test_nested_group_typo_names_the_group_and_suggests():
    text = _parse_error(["gateway", "stat"])
    assert "'stat' is not a `hermes gateway` command" in text
    assert "Did you mean:" in text and "status" in text

def test_invalid_profile_flag_value_explains_rule_and_exits(monkeypatch):
    from hermes_cli import main as _main

    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "Work Bot", "status"])
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        _main._apply_profile_override()
    assert exc.value.code == 2

def test_pytest_style_dash_p_is_still_ignored(monkeypatch):
    from hermes_cli import main as _main

    monkeypatch.setattr(sys, "argv", ["pytest", "-p", "no:xdist", "tests/"])
    assert _main._scan_profile_flag(sys.argv[1:]) == (None, 0, None)

def test_option_looking_dash_p_value_is_a_silent_skip_even_under_hermes(monkeypatch):
    from hermes_cli import main as _main

    # `-p no:xdist` reaching us through a differently named runner (tox, nox, python -m) must not exit.
    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "no:xdist", "tests/"])
    assert _main._scan_profile_flag(sys.argv[1:]) == (None, 0, None)
    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "--flag"])
    assert _main._scan_profile_flag(sys.argv[1:]) == (None, 0, None)

def test_title_cased_profile_label_is_normalised_not_rejected(monkeypatch):
    from hermes_cli import main as _main

    assert _main._scan_profile_flag(["-p", " Work ", "status"]) == ("work", 2, 0)
    assert _main._scan_profile_flag(["--profile=Work", "status"]) == ("work", 1, 0)

def test_invalid_dash_p_after_a_subcommand_is_left_to_that_subcommand(monkeypatch):
    from hermes_cli import main as _main

    # A plugin/subcommand flag such as `hermes kanban serve -p "Work Bot"` is not our profile selector.
    monkeypatch.setattr(sys, "argv", ["hermes", "kanban", "serve", "-p", "Work Bot"])
    assert _main._scan_profile_flag(sys.argv[1:]) == (None, 0, None)


_REPO = Path(__file__).resolve().parents[2]
_FOREIGN_IMPORT = """\
import os
import sys

home, argv0 = sys.argv[1], sys.argv[2]
os.environ["HERMES_HOME"] = home
# pytest's separate ``-p <plugin>`` (and another entry point's same shape).
sys.argv = [
    argv0,
    "tests/hermes_cli/test_codex_credential_host_binding.py",
    "-p",
    "usage_guard_probe",
    "--tb=short",
]
import hermes_cli.main

assert os.environ["HERMES_HOME"] == home, os.environ["HERMES_HOME"]
assert hermes_cli.main.explicit_cli_profile() is None
print("OK")
"""


@pytest.mark.parametrize("argv0", [
    "/usr/lib/python3.14/site-packages/pytest/__main__.py",
    "/opt/ci/run_tests_parallel.py",
])
def test_importing_main_with_foreign_separate_dash_p_does_not_resolve_a_profile(tmp_path, argv0):
    """Importing hermes_cli.main must not treat another program's ``-p <plugin>`` as ``--profile``.

    ``usage_guard_probe`` is a legal profile id, so the colon/dash skip for
    ``-p no:xdist`` does not cover it. The import must not look the name up
    or SystemExit.
    """
    home = tmp_path / "profiles" / "default"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    probe = tmp_path / "probe_import.py"
    probe.write_text(_FOREIGN_IMPORT, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(_REPO)]
    )
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(probe), str(home), argv0],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage_guard_probe" not in result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_apply_ignores_foreign_separate_dash_p_without_a_profile_lookup(tmp_path, monkeypatch):
    from hermes_cli import main as _main

    home = tmp_path / "profiles" / "default"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(sys, "argv", [
        "/opt/ci/run_tests_parallel.py",
        "tests/hermes_cli/test_codex_credential_host_binding.py",
        "-p",
        "usage_guard_probe",
        "--tb=short",
    ])
    looked_up: list[str] = []

    def _boom(name: str) -> str:
        looked_up.append(name)
        raise AssertionError(f"profile lookup of {name}")

    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", _boom)
    _main._apply_profile_override()
    assert looked_up == []
    assert os.environ["HERMES_HOME"] == str(home)
    assert _main.explicit_cli_profile() is None


@pytest.mark.parametrize("argv0", ["hermes", "/opt/hermes/hermes_cli/main.py"])
def test_real_hermes_invocation_still_selects_dash_p(tmp_path, monkeypatch, argv0):
    from hermes_cli import main as _main

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(sys, "argv", [argv0, "-p", "work", "status"])
    _main._apply_profile_override()
    assert os.environ["HERMES_HOME"] == str(profile)
    assert _main.explicit_cli_profile() == "work"
