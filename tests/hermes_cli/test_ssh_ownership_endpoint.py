import os
import types

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


def test_ssh_ownership_endpoint_requires_token_and_returns_exact_nonce(monkeypatch):
    token = "t" * 64
    nonce = "0123456789abcdef"
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(web_server, "_SSH_OWNER_NONCE", nonce)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)

    assert client.get("/api/ssh/ownership").status_code == 401
    response = client.get(
        "/api/ssh/ownership",
        headers={"X-Hermes-Session-Token": token},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "sshOwnerNonce": nonce,
        "protocolVersion": 1,
        "runtimeIntact": True,
    }


def test_ssh_ownership_reports_replaced_runtime(tmp_path, monkeypatch):
    token = "t" * 64
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(web_server, "_SSH_OWNER_NONCE", "0123456789abcdef")
    monkeypatch.setattr(web_server, "_SSH_RUNTIME_MARKER", None)
    # A REAL purelib file whose recorded inode deliberately mismatches what
    # os.stat now reports — never patch os.stat globally here: web_server.os
    # is the os module itself, and swapping its stat() poisons every other
    # thread in this worker process (daemon threads from earlier tests crash
    # in their excepthooks → nondeterministic teardown errors across the
    # whole suite, the Aug 2026 CI flake).
    purelib = tmp_path / "site-packages"
    purelib.mkdir()
    st = purelib.stat()
    monkeypatch.setattr(
        web_server, "_SSH_RUNTIME_PURELIB", (str(purelib), st.st_dev, st.st_ino + 1)
    )
    client = TestClient(web_server.app)

    response = client.get("/api/ssh/ownership", headers={"X-Hermes-Session-Token": token})

    assert response.status_code == 200
    assert response.json()["runtimeIntact"] is False


def test_ssh_runtime_marker_detects_recreated_venv_even_with_reused_inode(
    tmp_path, monkeypatch
):
    """The exact #82429 repro: rm -rf venv && recreate. On ext4 the new
    site-packages directory routinely REUSES the old inode (proven live
    during salvage), so the stat snapshot alone reports intact. The marker
    file is the deterministic tier: it dies with the old tree."""
    purelib = tmp_path / "venv" / "lib" / "site-packages"
    purelib.mkdir(parents=True)
    # Swap the MODULE ATTRIBUTE on web_server, not sysconfig.get_paths itself:
    # sysconfig is process-global, and mutating it races every other thread
    # in the worker (same cross-thread poisoning class as the os.stat patch
    # this file used to have).
    monkeypatch.setattr(
        web_server,
        "sysconfig",
        types.SimpleNamespace(get_paths=lambda *a, **k: {"purelib": str(purelib)}),
    )

    web_server._apply_ssh_owner_nonce("0123456789abcdef")
    try:
        assert web_server._ssh_runtime_intact() is True

        # Replace the venv; the recreated directory may reuse the inode.
        import shutil

        shutil.rmtree(tmp_path / "venv")
        purelib.mkdir(parents=True)

        assert web_server._ssh_runtime_intact() is False, (
            "marker tier must catch a recreated venv regardless of inode reuse"
        )
    finally:
        web_server._apply_ssh_owner_nonce(None)


def _pkg(purelib, name, record):
    info = purelib / f"{name}.dist-info"
    info.mkdir()
    (info / "RECORD").write_text(record, encoding="utf-8")


def test_ssh_runtime_reattaches_only_when_the_rerealized_tree_holds_the_same_bytes(tmp_path, monkeypatch):
    """[daveey] A PM repair re-realizes an identical tree: the marker dies with the old tree, but the
    backend's runtime did not change, so it writes the marker back and stays reusable instead of
    retiring. A tree whose packages changed is a real swap: no reattach, the watchdog retires."""
    import shutil

    purelib = tmp_path / "py" / "lib" / "site-packages"
    purelib.mkdir(parents=True)
    _pkg(purelib, "pip-26.2.1", "pip/__init__.py,sha256=aaa,10\n")
    monkeypatch.setattr(
        web_server,
        "sysconfig",
        types.SimpleNamespace(get_paths=lambda *a, **k: {"purelib": str(purelib)}),
    )
    web_server._apply_ssh_owner_nonce("0123456789abcdef")
    marker = purelib / ".hermes-ssh-runtime-0123456789abcdef"
    try:
        assert web_server._ssh_runtime_reattach() is False  # nothing lost: nothing to do

        shutil.rmtree(tmp_path / "py")
        purelib.mkdir(parents=True)
        _pkg(purelib, "pip-26.2.1", "pip/__init__.py,sha256=aaa,10\n")
        assert web_server._ssh_runtime_intact() is False
        assert web_server._ssh_runtime_reattach() is True
        assert marker.is_file()
        assert web_server._ssh_runtime_intact() is True

        shutil.rmtree(tmp_path / "py")
        purelib.mkdir(parents=True)
        _pkg(purelib, "pip-26.3.0", "pip/__init__.py,sha256=bbb,10\n")
        assert web_server._ssh_runtime_reattach() is False
        assert not marker.exists()
        assert web_server._ssh_runtime_intact() is False
    finally:
        web_server._apply_ssh_owner_nonce(None)


def test_ssh_runtime_marker_name_is_the_shape_pm_digests_skip():
    """The marker lives in a PM-published tree; PM must treat exactly this name as process state."""
    from pm.store import is_runtime_state_file

    assert is_runtime_state_file(".hermes-ssh-runtime-0123456789abcdef")
    assert not is_runtime_state_file(".hermes-ssh-runtime-0123456789abcdef.py")


def test_ssh_runtime_marker_survives_in_place_installs(tmp_path, monkeypatch):
    """pip/uv installs INTO the live venv must not read as a replacement."""
    purelib = tmp_path / "venv" / "lib" / "site-packages"
    purelib.mkdir(parents=True)
    # Swap the MODULE ATTRIBUTE on web_server, not sysconfig.get_paths itself:
    # sysconfig is process-global, and mutating it races every other thread
    # in the worker (same cross-thread poisoning class as the os.stat patch
    # this file used to have).
    monkeypatch.setattr(
        web_server,
        "sysconfig",
        types.SimpleNamespace(get_paths=lambda *a, **k: {"purelib": str(purelib)}),
    )

    web_server._apply_ssh_owner_nonce("0123456789abcdef")
    try:
        (purelib / "newpkg").mkdir()  # a package landing in the live venv
        assert web_server._ssh_runtime_intact() is True
    finally:
        web_server._apply_ssh_owner_nonce(None)


@pytest.mark.platforms("linux")
def test_ssh_runtime_readonly_purelib_falls_back_to_stat(tmp_path, monkeypatch):
    """When site-packages is unwritable the marker can't be placed; the
    stat-snapshot fallback still arms (weaker, never a false stale)."""
    purelib = tmp_path / "venv" / "lib" / "site-packages"
    purelib.mkdir(parents=True)
    # Swap the MODULE ATTRIBUTE on web_server, not sysconfig.get_paths itself:
    # sysconfig is process-global, and mutating it races every other thread
    # in the worker (same cross-thread poisoning class as the os.stat patch
    # this file used to have).
    monkeypatch.setattr(
        web_server,
        "sysconfig",
        types.SimpleNamespace(get_paths=lambda *a, **k: {"purelib": str(purelib)}),
    )
    # Make the directory REALLY unwritable instead of patching builtins.open:
    # a global open() patch races every other thread in the worker process
    # (daemon threads crash in their excepthooks → nondeterministic teardown
    # errors file-wide, the Aug 2026 CI flake). chmod is thread-safe and
    # exercises the genuine OSError path.
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        pytest.skip("directory write bits are not enforced for root")
    purelib.chmod(0o555)
    try:
        web_server._apply_ssh_owner_nonce("0123456789abcdef")
        try:
            assert web_server._SSH_RUNTIME_MARKER is None
            assert web_server._SSH_RUNTIME_PURELIB is not None
            assert web_server._ssh_runtime_intact() is True
        finally:
            web_server._apply_ssh_owner_nonce(None)
    finally:
        purelib.chmod(0o755)  # let tmp_path cleanup succeed


def test_ssh_ownership_endpoint_is_absent_without_owner_nonce(monkeypatch):
    token = "t" * 64
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(web_server, "_SSH_OWNER_NONCE", None)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)

    response = client.get(
        "/api/ssh/ownership",
        headers={"X-Hermes-Session-Token": token},
    )
    assert response.status_code == 404


def _purelib_at(tmp_path, monkeypatch):
    purelib = tmp_path / "venv" / "lib" / "site-packages"
    purelib.mkdir(parents=True)
    monkeypatch.setattr(
        web_server,
        "sysconfig",
        types.SimpleNamespace(get_paths=lambda *a, **k: {"purelib": str(purelib)}),
    )
    return purelib


def test_serve_that_exits_cleanly_leaves_no_runtime_marker(tmp_path):
    """A real interpreter that boots as an SSH-isolated serve and exits normally (atexit path)."""
    import subprocess
    import sys
    import textwrap

    purelib = tmp_path / "site-packages"
    purelib.mkdir()
    script = textwrap.dedent(
        f"""
        import types
        from hermes_cli import web_server
        web_server.sysconfig = types.SimpleNamespace(
            get_paths=lambda *a, **k: {{"purelib": {str(purelib)!r}}})
        web_server._apply_ssh_owner_nonce("0123456789abcdef")
        import os
        assert os.path.exists({str(purelib / ".hermes-ssh-runtime-0123456789abcdef")!r})
        """
    )
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    subprocess.run([sys.executable, "-c", script], cwd=repo, check=True, timeout=120)
    assert list(purelib.iterdir()) == []


def test_release_removes_own_marker_but_not_one_rewritten_by_another_process(tmp_path, monkeypatch):
    purelib = _purelib_at(tmp_path, monkeypatch)
    marker = purelib / ".hermes-ssh-runtime-0123456789abcdef"
    web_server._apply_ssh_owner_nonce("0123456789abcdef")
    try:
        assert marker.exists()
        marker.write_text(f"pid={os.getpid() + 1}\n")  # a successor serve reused the nonce
        web_server._release_ssh_runtime_marker()
        assert marker.exists()
        marker.write_text(f"pid={os.getpid()}\n")
        web_server._release_ssh_runtime_marker()
        assert not marker.exists()
    finally:
        web_server._apply_ssh_owner_nonce(None)


def test_prune_removes_dead_serve_markers_and_leaves_live_ones(tmp_path, monkeypatch):
    import subprocess
    import sys

    purelib = _purelib_at(tmp_path, monkeypatch)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stale = purelib / ".hermes-ssh-runtime-aaaaaaaaaaaaaaaa"
        held = purelib / ".hermes-ssh-runtime-bbbbbbbbbbbbbbbb"
        odd = purelib / ".hermes-ssh-runtime-cccccccccccccccc"
        unrelated = purelib / ".hermes-ssh-runtime-notanonce"
        stale.write_text(f"pid={dead.pid}\n")
        held.write_text(f"pid={live.pid}\n")
        odd.write_text("garbage")
        unrelated.write_text(f"pid={dead.pid}\n")

        web_server._apply_ssh_owner_nonce("0123456789abcdef")
        try:
            assert not stale.exists()
            assert held.exists()
            assert odd.exists()
            assert unrelated.exists()
            assert (purelib / ".hermes-ssh-runtime-0123456789abcdef").exists()
        finally:
            web_server._apply_ssh_owner_nonce(None)
    finally:
        live.kill()
        live.wait()


def test_lifespan_shutdown_calls_release_ssh_runtime_marker(tmp_path, monkeypatch):
    """Graceful serve shutdown goes through FastAPI lifespan, not only atexit.

    The subprocess exit test covers atexit. This one fails if the lifespan
    finally block stops calling _release_ssh_runtime_marker: TestClient does
    not run atexit, so the marker would still be there after the server stops.
    """
    from fastapi.testclient import TestClient
    from hermes_cli import free_tier_bootstrap
    from hermes_cli.local_runtime import bootstrap as local_runtime_bootstrap
    from tools import process_registry_readopt
    from tui_gateway import methods_groups, startup_resume

    purelib = _purelib_at(tmp_path, monkeypatch)
    nonce = "0123456789abcdef"
    marker = purelib / f".hermes-ssh-runtime-{nonce}"
    calls = []
    real_release = web_server._release_ssh_runtime_marker

    def _spy():
        calls.append("release")
        real_release()

    # Startup side effects are not what this test covers, and on a live host
    # they must not resume sessions or boot a local runtime.
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.setattr(web_server, "_warm_gateway_module", lambda: None)
    monkeypatch.setattr(web_server, "_eager_reconcile_own_session_db", lambda: None)
    monkeypatch.setattr(free_tier_bootstrap, "start_background_bootstrap", lambda: None)
    monkeypatch.setattr(local_runtime_bootstrap, "ensure_local_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(process_registry_readopt, "start_checkpoint_keeper", lambda: None)
    monkeypatch.setattr(process_registry_readopt, "start_tui_orphan_recovery", lambda: None)
    monkeypatch.setattr(methods_groups, "start_hosted_room_service", lambda: None)
    monkeypatch.setattr(methods_groups, "stop_hosted_room_service", lambda **_k: True)
    monkeypatch.setattr(startup_resume, "start_startup_resume_sweep", lambda: None)

    web_server._apply_ssh_owner_nonce(nonce)
    try:
        assert marker.read_text(encoding="utf-8") == f"pid={os.getpid()}\n"
        # atexit captured the original function. The lifespan looks the name
        # up at shutdown, so only that path hits the spy.
        monkeypatch.setattr(web_server, "_release_ssh_runtime_marker", _spy)
        with TestClient(web_server.app, raise_server_exceptions=False):
            assert web_server._SSH_RUNTIME_MARKER == str(marker)
            assert marker.exists()
            assert calls == []
        assert calls == ["release"]
        assert not marker.exists()
    finally:
        web_server._apply_ssh_owner_nonce(None)
