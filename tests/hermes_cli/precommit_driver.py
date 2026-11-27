"""Disposable updater adapter: production command/protocol, only external effects replaced.

Executed by composed state tests with an explicit source root, checkout and home.
Never suitable for a live checkout: requires the fixture's .precommit-fixture marker.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading


def install(source: str, checkout: str, mode: str) -> None:
    """Install external-effect adapters in EACH real updater interpreter."""
    sys.path.insert(0, source)
    from hermes_cli import main, update_cmd, update_cmd_posix_pause as pause
    from hermes_cli import update_precommit as protocol
    root = Path(checkout)
    if not (root / ".precommit-fixture").is_file():
        raise RuntimeError("disposable checkout marker required")
    main.PROJECT_ROOT = root
    main._run_pre_update_backup = lambda *_: None
    main._update_preflight_handled = lambda *_: False
    main._install_hangup_protection = lambda **_: None
    main._finalize_update_output = lambda *_: None
    main._pause_windows_gateways_for_update = lambda: None
    main._resume_windows_gateways_after_update = lambda *_: None
    main._finalize_update_receipt = lambda *_: None
    pause._discover_systemd = list
    pause._discover_launchd = list
    pause._discover_bare = lambda *_: ([], [])
    pause._left_running = lambda units, jobs: (units, jobs, [])
    options = update_cmd._UpdateOptions(pre_update_version=None, gw_input_fn=None,
        assume_yes=True, keep_stash=False, switch_branch=False, discard_local_changes=False)
    update_cmd._resolve_update_options = lambda *_: options
    update_cmd._begin_update_receipt_and_plan = lambda *_: None
    real_git = update_cmd._git_run

    def git_run(git_cmd, args, **kwargs):
        if mode == "fetch" and args[0] == "fetch":
            (root.parent / "fetch-entered").write_text(str(protocol.clock()), encoding="utf-8")
            from hermes_cli.update_custody import run_git
            return run_git([sys.executable, "-c", "import threading; threading.Event().wait(300)"],
                           [], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=300)
        return real_git(git_cmd, args, **kwargs)
    update_cmd._git_run = git_run

    def complete(request):
        if mode == "apply":
            threading.Event().wait(max(0, protocol._child_deadline - protocol.clock()) + 0.2)  # health: allow HX013 -- postcommit expiry IS the exercised behavior
        (root.parent / "completed").write_text(request["expected_sha"], encoding="utf-8")
    update_cmd._complete_source_update = complete


def run() -> None:
    source, checkout, mode, *argv = sys.argv[1:]
    install(source, checkout, mode)
    from hermes_cli import main, update_precommit as protocol
    from hermes_cli.subcommands.update import build_update_parser
    real_supervise = protocol.supervise
    driver = str(Path(__file__).resolve())

    def supervise(command, deadline, env):
        # Keep production executable, payload, fd/deadline arguments and child_entry.
        # Install the same external-effect adapters before importing the command.
        bootstrap = (f"import runpy; runpy.run_path({driver!r})['install']"
                     f"({source!r}, {checkout!r}, {mode!r}); ")
        command[2] = bootstrap + command[2]
        return real_supervise(command, deadline, env)
    protocol.supervise = supervise
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    args = parser.parse_args(argv)
    (Path(checkout).parent / "launch.json").write_text(json.dumps(argv), encoding="utf-8")
    main.cmd_update(args)


if __name__ == "__main__":
    run()
