"""The wire harness must not launch unrelated installers into its disposable home."""
import os
from pathlib import Path
import subprocess
import threading

from evals import mcp_device_flow
from hermes_cli import default_plugins
from pm import environments, plugins_state


def test_wire_home_disables_detached_installers_before_cleanup(monkeypatch):
    release = threading.Event()
    written = threading.Event()
    writers = []
    homes = []
    errors = []
    original_rmdir = os.rmdir

    def delayed_install(home):
        try:
            assert release.wait(10), "cleanup never reached the cache directory"
            (home / "cache" / "late-install").write_text("fixture")
        except (AssertionError, OSError) as exc:
            errors.append(exc)
        finally:
            written.set()

    def spawn_install():
        home = homes[-1]
        (home / "cache").mkdir(exist_ok=True)
        writer = threading.Thread(target=delayed_install, args=(home,))
        writers.append(writer)
        writer.start()
        return True

    def cli_startup(argv, **kwargs):
        # Exercise the real boot policy on the exact home run_cli passes to its child.
        # Auth protocol and nondisclosure are covered by test_mcp_device_flow.
        home = Path(kwargs["env"]["HERMES_HOME"])
        homes.append(home)
        with monkeypatch.context() as scoped:
            scoped.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
            scoped.setattr(plugins_state, "dependency_homes", lambda: [home])
            scoped.setattr(environments, "dependency_home_root", lambda: home)
            scoped.setattr(default_plugins, "spawn_install", spawn_install)
            outcome = default_plugins.step_install_default_plugins()
        assert outcome["ok"], outcome
        return subprocess.CompletedProcess(argv, 0, "Authentication failed", "")

    def remove_after_writer(path, *args, **kwargs):
        # Force the observed interleaving: scandir has finished, but rmdir has not.
        if writers and Path(os.fspath(path)).name == "cache":
            release.set()
            assert written.wait(10), "installer did not finish its write"
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(mcp_device_flow.subprocess, "run", cli_startup)
    monkeypatch.setattr(os, "rmdir", remove_after_writer)
    try:
        mcp_device_flow.run_cli(Path(__file__).resolve().parents[2], "unsupported")
    finally:
        release.set()
        for writer in writers:
            writer.join(10)
            assert not writer.is_alive()
        # A failing RED run still owns and cleans its controlled writer's files.
        with monkeypatch.context() as scoped:
            scoped.setattr(os, "rmdir", original_rmdir)
            import shutil
            for home in homes:
                if home.exists():
                    shutil.rmtree(home)
    assert not errors
    assert not writers, "an unrelated installer outlives the CLI"
    assert homes and all(not home.exists() for home in homes)
