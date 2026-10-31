"""Behavior of scripts/ci/plugin_validation_sandbox.py, the boundary that keeps the
catalog job's GH_TOKEN (and every other host credential) away from plugin code that
``hermes plugins validate --install-deps`` runs.

Every case executes the real wrapper. The isolation cases need a host where an
unprivileged process can create user namespaces (``bwrap --unshare-user
--unshare-net``). Every Linux CI lane of the fork runs on the runner AMI
(hosts/hermes-ci-runners/packer/provision.sh), which ships bwrap with the AppArmor
restriction lifted, so under GitHub Actions on Linux they must run: a runner that
cannot isolate fails them instead of skipping. Elsewhere (a developer's Ubuntu
24.04 with the restriction on) they skip with the kernel's reason. The fail-closed
and refusal cases need no bwrap and run everywhere.

``PLUGIN_SANDBOX_E2E=1`` additionally drives the real ``hermes plugins validate
--install-deps`` through the wrapper against a PM home prepared by setup-pm
(``HERMES_HOME``/``VIRTUAL_ENV``/``PYTHONPATH`` as the catalog job exports them).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "plugin_validation_sandbox.py"
SENTINEL = "ghs_PLUGIN_SANDBOX_SENTINEL"


def _load():
    spec = importlib.util.spec_from_file_location("plugin_validation_sandbox", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sandbox = _load()


def run(*args: str, env: dict | None = None, timeout: int = 120, **kw) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], env=full_env, timeout=timeout,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, **kw)


def _isolation_unavailable() -> str | None:
    """Why this host cannot run the isolation cases, judged by bwrap ITSELF with the
    wrapper's namespace flags, never by the wrapper: a broken wrapper must fail these
    tests, not skip them."""
    if sys.platform != "linux":
        return "Linux only"
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return "bubblewrap is not installed"
    argv = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev"]
    for top in ("bin", "lib", "lib64"):
        host = Path("/") / top
        if host.is_symlink():
            argv += ["--symlink", os.readlink(host), f"/{top}"]
        elif host.is_dir():
            argv += ["--ro-bind", str(host), f"/{top}"]
    r = subprocess.run([*argv, "true"], capture_output=True, text=True, timeout=60, check=False)
    if r.returncode != 0:
        return f"this kernel refuses unprivileged user/net namespaces: {r.stderr.strip()[-200:]}"
    return None


ISOLATION_SKIP = _isolation_unavailable()
# CI (scripts/run_tests.sh forwards GITHUB_ACTIONS) never turns a missing sandbox
# into a green skip: the isolation cases run and fail on their own evidence.
REQUIRE_ISOLATION = sys.platform == "linux" and os.environ.get("GITHUB_ACTIONS") == "true"
needs_isolation = pytest.mark.skipif(ISOLATION_SKIP is not None and not REQUIRE_ISOLATION,
                                     reason=str(ISOLATION_SKIP))


@pytest.mark.skipif(not REQUIRE_ISOLATION, reason="only CI (GITHUB_ACTIONS on Linux) must provide isolation")
def test_ci_linux_runners_can_isolate():
    """The one-line reason a CI runner fails the isolation cases below."""
    assert ISOLATION_SKIP is None, f"this CI runner cannot run the plugin sandbox: {ISOLATION_SKIP}"


@pytest.fixture
def work(tmp_path):
    """A scratch dir the wrapper may expose. tmp_path can sit inside a refused tree
    (a developer's ~/.hermes scratch), so fall back to /var/tmp there."""
    try:
        sandbox.check_bind_path(str(tmp_path), dict(os.environ))
        yield tmp_path
        return
    except sandbox.SetupError:
        pass
    path = Path(tempfile.mkdtemp(prefix="sandbox-test-", dir="/var/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _py(code: str) -> list[str]:
    # The base interpreter (not a venv shim), isolated: only the stdlib is needed.
    return [os.path.realpath(sys.executable), "-I", "-c", textwrap.dedent(code)]


def _python_mounts() -> list[str]:
    """--ro mounts that make this interpreter usable inside the sandbox."""
    prefix = Path(os.path.realpath(sys.base_prefix))
    return [] if prefix.parts[1:2] == ("usr",) else ["--ro", str(prefix)]


def _public_https_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


# ─── fail closed (no bwrap needed) ──────────────────────────────────────────────

def test_broken_bwrap_never_runs_the_command(work):
    marker = work / "ran"
    r = run("--bwrap", "/bin/false", "--", "sh", "-c", f"touch {marker}")
    assert r.returncode != 0
    assert not marker.exists()


def test_missing_bwrap_is_a_setup_failure(work):
    marker = work / "ran"
    r = run("--bwrap", "", "--", "sh", "-c", f"touch {marker}", env={"PATH": str(work)})
    assert r.returncode == sandbox.EXIT_SETUP, r.stderr
    assert "bwrap" in r.stderr
    assert not marker.exists()


def test_a_bwrap_that_skips_the_namespaces_is_caught_by_the_inner_self_check(work):
    """A bwrap that 'succeeds' without isolating (wrong build, a shim) must not run
    the command: the in-sandbox init compares its namespaces with the host's."""
    marker = work / "ran"
    fake = work / "fake-bwrap"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        argv = sys.argv[1:]
        rest = argv[argv.index("--") + 1:]
        rest = [{str(SCRIPT)!r} if a == {sandbox.INNER_SCRIPT!r} else a for a in rest]
        os.execv(rest[0], rest)
        """))
    fake.chmod(0o755)
    r = run("--bwrap", str(fake), "--no-network", "--", "sh", "-c", f"touch {marker}",
            env={"GH_TOKEN": SENTINEL})
    assert r.returncode == sandbox.EXIT_SELFCHECK, (r.returncode, r.stderr)
    assert "namespace is shared with the host" in r.stderr
    assert not marker.exists()


def _deep_dir(base: Path) -> Path:
    """A real directory whose proxy.sock path is far past sun_path's 108 bytes,
    like the CI runner's pytest TMPDIR (/var/tmp/hermes-pytest-…/pytest-of-…/…)."""
    deep = base / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True, mode=0o700)
    assert len(str(deep / "plugin-sandbox-xxxxxxxx" / "proxy.sock")) > 120
    return deep


def test_the_proxy_binds_and_serves_under_a_deep_runner_temp(work):
    """Regression: a deep $RUNNER_TEMP/$TMPDIR made the wrapper die with 'AF_UNIX
    path too long' before bwrap ran. A fake bwrap stands in so this runs without
    namespaces: it gets the real host socket from `--bind <src> INNER_SOCKET`,
    checks it is 0600, and speaks CONNECT to it (the policy must still answer)."""
    deep = _deep_dir(work)
    result = work / "result.json"
    fake = work / "fake-bwrap"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, socket, stat, sys
        argv = sys.argv[1:]
        src = next(argv[i - 1] for i, a in enumerate(argv) if a == {sandbox.INNER_SOCKET!r})
        d = os.open(os.path.dirname(src), os.O_PATH | os.O_DIRECTORY)
        s = socket.socket(socket.AF_UNIX)
        s.connect(f"/proc/self/fd/{{d}}/{{os.path.basename(src)}}")
        s.sendall(b"CONNECT 169.254.169.254:443 HTTP/1.1\\r\\n\\r\\n")
        reply = s.recv(64).split(b"\\r\\n")[0].decode()
        st = os.stat(src)
        json.dump({{"src": src, "mode": oct(stat.S_IMODE(st.st_mode)),
                    "is_socket": stat.S_ISSOCK(st.st_mode), "reply": reply}},
                  open({str(result)!r}, "w"))
        """))
    fake.chmod(0o755)
    r = run("--bwrap", str(fake), "--", "true", env={"RUNNER_TEMP": str(deep)})
    assert "AF_UNIX" not in r.stderr, r.stderr
    assert r.returncode == 0, (r.returncode, r.stderr)
    seen = json.loads(result.read_text())
    assert seen["src"].startswith(str(deep) + "/plugin-sandbox-"), seen   # in its private scratch, not a shared dir
    assert seen["is_socket"] and seen["mode"] == "0o600", seen
    assert seen["reply"].startswith("HTTP/1.1 403"), seen
    assert "denied CONNECT 169.254.169.254:443" in r.stderr
    assert not list(deep.glob("plugin-sandbox-*")), "scratch left behind"


def test_the_proxy_socket_is_never_bound_through_a_symlinked_dir(work):
    """The short fd-relative bind opens the scratch dir O_NOFOLLOW: a symlink in
    its place is refused rather than followed to wherever it points."""
    deep = _deep_dir(work)
    target = work / "elsewhere"
    target.mkdir()
    link = deep / "scratch"
    link.symlink_to(target)
    with pytest.raises(OSError):
        sandbox.EgressProxy(str(link / "proxy.sock"), sandbox.EgressPolicy({443}, []))
    assert not (target / "proxy.sock").exists()


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "AWS_SECRET_ACCESS_KEY",
                                  "SSH_AUTH_SOCK", "RUNNER_TEMP", "NPM_TOKEN", "HTTPS_PROXY"])
def test_credential_env_names_are_refused_even_when_requested(work, name):
    marker = work / "ran"
    r = run("--env", name, "--", "sh", "-c", f"touch {marker}", env={name: SENTINEL})
    assert r.returncode == sandbox.EXIT_SETUP
    assert f"refusing to forward env {name}" in r.stderr
    assert not marker.exists()


def test_home_and_top_level_dirs_are_refused(work):
    for path in (os.path.expanduser("~"), "/", "/home", "/usr"):
        r = run("--ro", path, "--", "true")
        assert r.returncode == sandbox.EXIT_SETUP, path
        assert "refusing to expose" in r.stderr, (path, r.stderr)


def test_runner_file_commands_and_credential_stores_are_refused(work):
    temp = work / "_temp"
    cmds = temp / "_runner_file_commands"
    cmds.mkdir(parents=True)
    (cmds / "set_env_x").write_text("")
    (temp / "setup-pm").mkdir()
    env = {"GITHUB_ENV": str(cmds / "set_env_x")}
    r = run("--overlay", str(temp), "--", "true", env=env)
    assert r.returncode == sandbox.EXIT_SETUP and "contains" in r.stderr, r.stderr
    # A sibling of the file-command dir is fine as far as this rule goes.
    sandbox.check_bind_path(str(temp / "setup-pm"), {**os.environ, **env})

    home = work / "home"
    (home / ".ssh" / "keys").mkdir(parents=True)
    r = run("--ro", str(home / ".ssh" / "keys"), "--", "true", env={"HOME": str(home)})
    assert r.returncode == sandbox.EXIT_SETUP and "inside" in r.stderr, r.stderr


def test_a_checkout_with_persisted_git_credentials_is_refused(work):
    repo = work / "checkout"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic eC1hY2Nlc3M=\n')
    r = run("--ro", str(repo), "--", "true")
    assert r.returncode == sandbox.EXIT_SETUP
    assert ".git/config carries credentials" in r.stderr


def _interpreters() -> list[str]:
    """This interpreter, plus the system one a runner's bare `python3 -I` may be
    (Ubuntu 24.04: 3.12, whose Path.is_dir raises PermissionError where 3.14 says False)."""
    out = [os.path.realpath(sys.executable)]
    system = "/usr/bin/python3"
    if os.access(system, os.X_OK) and os.path.realpath(system) not in out:
        out.append(os.path.realpath(system))
    return out


@pytest.mark.parametrize("python", _interpreters())
def test_unstatable_secret_paths_do_not_crash_the_path_policy(work, python):
    """A non-root runner cannot stat inside /root (or another unreadable home): the
    policy must still decide (bwrap is reached here), never die with a traceback,
    and a .git/config it cannot read is refused rather than trusted."""
    locked_home = work / "locked-home"
    (locked_home / ".ssh").mkdir(parents=True)
    exposed = work / "exposed"
    exposed.mkdir()
    unreadable = work / "unreadable-checkout"
    (unreadable / ".git").mkdir(parents=True)
    (unreadable / ".git" / "config").write_text("[core]\n")
    (unreadable / ".git" / "config").chmod(0)
    locked_home.chmod(0)
    try:
        env = {**os.environ, "HOME": str(locked_home)}
        r = subprocess.run([python, "-I", str(SCRIPT), "--bwrap", "/bin/false", "--no-network",
                            "--ro", str(exposed), "--", "true"], env=env, timeout=120,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        assert "Traceback" not in r.stderr, r.stderr
        assert r.returncode == 1, (r.returncode, r.stderr)   # /bin/false as bwrap: the policy passed
        r = subprocess.run([python, "-I", str(SCRIPT), "--bwrap", "/bin/false", "--no-network",
                            "--ro", str(unreadable), "--", "true"], env=env, timeout=120,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        assert "Traceback" not in r.stderr, r.stderr
        if os.getuid() != 0:  # root reads a mode-000 file
            assert r.returncode == sandbox.EXIT_SETUP, (r.returncode, r.stderr)
            assert "cannot vet its .git/config" in r.stderr
    finally:
        locked_home.chmod(0o700)
        (unreadable / ".git" / "config").chmod(0o600)


# ─── egress policy (no bwrap needed) ────────────────────────────────────────────

@pytest.mark.parametrize("host,port,reason", [
    ("169.254.169.254", 443, "non-public"),   # EC2/GCP/Azure metadata
    ("127.0.0.1", 443, "non-public"),
    ("localhost", 443, "non-public"),
    ("10.0.0.5", 443, "non-public"),
    ("100.64.1.1", 443, "non-public"),        # CGNAT / Tailscale
    ("[::1]", 443, "non-public"),
    ("[::ffff:169.254.169.254]", 443, "non-public"),
    ("[fd00:ec2::254]", 443, "non-public"),   # EC2 metadata over IPv6
    # IPv6 spellings of an internal IPv4 that `ipaddress` calls global: NAT64 (a VPC
    # with DNS64 routes 64:ff9b::/96 to a NAT gateway that dials the IPv4 inside the
    # VPC) and IPv4-compatible.
    ("[64:ff9b::a9fe:a9fe]", 443, "non-public"),
    ("[64:ff9b::a00:5]", 443, "non-public"),
    ("[::a9fe:a9fe]", 443, "non-public"),
    ("2852039166", 443, "non-public"),         # 169.254.169.254 as one decimal number
    ("0x7f.1", 443, "non-public"),             # 127.0.0.1 in inet_aton shorthand
    ("pypi.org", 22, "port 22 not allowed"),
    ("bad host", 443, "malformed"),
])
def test_egress_policy_denies_internal_targets_and_other_ports(host, port, reason):
    verdict = sandbox.EgressPolicy({443}, []).resolve(host, port)
    assert isinstance(verdict, str) and reason in verdict, verdict


def test_nat64_of_a_public_ipv4_stays_allowed():
    """The embedded-IPv4 rule judges the IPv4, it does not ban NAT64 outright."""
    verdict = sandbox.EgressPolicy({443}, []).resolve("[64:ff9b::808:808]", 443)  # 8.8.8.8
    assert verdict == [("64:ff9b::808:808", 443)], verdict


def test_a_dial_that_lands_on_this_host_is_refused():
    """Policy is judged on addresses; a runner whose public address sits on its own
    interface would still be 'public'. The dial itself is refused when the kernel
    routed it to a local address (src == dst)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("0.0.0.0", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    targets = ["127.0.0.1"]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet: only picks the primary interface address
        targets.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for ip in targets:
            assert sandbox._dial([(ip, port)]) == f"{ip} is this host", ip
    finally:
        listener.close()


@pytest.mark.skipif(not _public_https_reachable(), reason="host has no route to pypi.org:443")
def test_a_dial_to_a_public_host_is_not_mistaken_for_this_host():
    addresses = sandbox.EgressPolicy({443}, []).resolve("pypi.org", 443)
    assert isinstance(addresses, list), addresses
    upstream = sandbox._dial(addresses)
    assert isinstance(upstream, socket.socket), upstream
    upstream.close()


def test_scratch_removal_survives_dirs_the_sandbox_made_unlistable(work):
    """The overlay copy is writable by plugin code, which can chmod 000 its dirs; the
    runner user (not root) must still remove it, or every entry leaves a PM-home copy."""
    scratch = work / "plugin-sandbox-x"
    deep = scratch / "copy0" / "home" / "trap" / "deep"
    deep.mkdir(parents=True)
    (deep / "f").write_text("x")
    for d in (deep, deep.parent, deep.parent.parent):
        d.chmod(0)
    sandbox.remove_tree(scratch)
    assert not scratch.exists()


def test_egress_host_allowlist_matches_domains_and_subdomains_only():
    policy = sandbox.EgressPolicy({443}, ["pythonhosted.org", "pypi.org"])
    assert policy.host_allowed("files.pythonhosted.org")
    assert policy.host_allowed("pypi.org")
    assert not policy.host_allowed("evilpypi.org")
    assert not policy.host_allowed("pypi.org.evil.com")
    assert policy.resolve("example.com", 443) == "host not allowed"


# ─── isolation (needs unprivileged namespaces) ──────────────────────────────────

@needs_isolation
def test_token_bearing_same_user_process_and_host_files_are_invisible(work):
    """The job shell holds GH_TOKEN; a plugin running as the same uid must see no
    process of the host (so no /proc/<pid>/environ, no ptrace/kill target), no
    unbound host file, and no credential in its own environment."""
    secret_dir = work / "secret"
    secret_dir.mkdir()
    (secret_dir / "token").write_text(SENTINEL)
    exposed = work / "exposed"
    exposed.mkdir()
    sibling = subprocess.Popen(["sleep", "120"], env={**os.environ, "GH_TOKEN": SENTINEL})
    # A host-side abstract Unix socket (no filesystem path, so no bind can hide it):
    # only a separate network namespace keeps it out of reach.
    abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abstract_name = f"\0plugin-sandbox-test-{os.getpid()}"
    abstract.bind(abstract_name)
    abstract.listen(1)
    try:
        probe = f"""
            import ctypes, glob, json, os, socket
            def read(p):
                try:
                    return open(p, "rb").read()
                except OSError:
                    return b""
            try:
                os.kill({sibling.pid}, 0); kill = "reachable"
            except ProcessLookupError:
                kill = "no such process"
            except PermissionError:
                kill = "permission"
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect({abstract_name!r}); abstract = "connected"
            except OSError as exc:
                abstract = type(exc).__name__
            libc = ctypes.CDLL(None, use_errno=True)
            nested_userns = libc.unshare(0x10000000) == 0  # CLONE_NEWUSER: the way back to capabilities
            print(json.dumps({{
                "environ_hits": [p for p in glob.glob("/proc/*/environ") if {SENTINEL!r}.encode() in read(p)],
                "cmdline": sorted(read(p).replace(b"\\0", b" ").decode() for p in glob.glob("/proc/[0-9]*/cmdline")),
                "own_env": {SENTINEL!r} in json.dumps(dict(os.environ)),
                "secret_file": os.path.exists({str(secret_dir / "token")!r}),
                "exposed": os.path.isdir({str(exposed)!r}),
                "home": os.listdir(os.environ["HOME"]),
                "kill_sibling": kill,
                "docker_sock": os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock"),
                "abstract_socket": abstract,
                "nested_userns": nested_userns,
                "sys": os.path.exists("/sys/fs/cgroup"),
            }}))
        """
        r = run(*_python_mounts(), "--ro", str(exposed), "--no-network", "--", *_py(probe),
                env={"GH_TOKEN": SENTINEL, "GITHUB_TOKEN": SENTINEL})
        assert r.returncode == 0, r.stderr
        seen = json.loads(r.stdout.strip().splitlines()[-1])
        assert seen["environ_hits"] == []
        assert not any("sleep 120" in c for c in seen["cmdline"]), seen["cmdline"]
        assert len(seen["cmdline"]) <= 4, seen["cmdline"]   # bwrap init, the init script, the probe
        assert seen["own_env"] is False
        assert seen["secret_file"] is False
        assert seen["exposed"] is True
        assert seen["home"] == []
        assert seen["kill_sibling"] == "no such process"
        assert seen["docker_sock"] is False
        assert seen["abstract_socket"] == "ConnectionRefusedError"
        assert seen["nested_userns"] is False
        assert seen["sys"] is False
    finally:
        abstract.close()
        sibling.kill()
        sibling.wait()


def _unique_sleep() -> str:
    """A sleep duration no other process uses, so its cmdline identifies it on the host."""
    return str(400000 + (os.getpid() * 7919 + time.time_ns()) % 500000)


def _alive(duration: str) -> list[str]:
    out = []
    for p in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if p.read_bytes() == f"sleep\0{duration}\0".encode():
                out.append(p.parent.name)
        except OSError:
            pass
    return out


@needs_isolation
def test_background_children_die_with_the_sandbox(work):
    """A plugin's install hook can fork a daemon that would wait for the token step;
    it must die when the validated command exits, even one that left the session."""
    n = _unique_sleep()
    proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--no-network", "--", "sh", "-c",
                             f"sleep {n} & (setsid sleep {n} &); sleep 3; exit 0"], stdin=subprocess.DEVNULL)
    wait_until(lambda: len(_alive(n)) == 2, timeout=20, what="both background children started")
    assert proc.wait(timeout=60) == 0
    wait_until(lambda: not _alive(n), timeout=10, what="background children dying with the sandbox")


@needs_isolation
def test_the_jobs_timeout_kills_the_whole_sandbox(work):
    """The job wraps the call in `timeout 600`: SIGTERM to the wrapper must take
    every sandboxed process down and remove the scratch dir."""
    n = _unique_sleep()
    home = work / "pm-home"
    home.mkdir()
    # The command keeps writing into its overlay copy while the wrapper is torn down.
    proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--no-network", "--overlay", str(home),
                             "--overlay-mode", "copy", "--", "sh", "-c",
                             f"sleep {n} & i=0; while :; do i=$((i+1)); mkdir -p {home}/w/$i; done & sleep {n}"],
                            env={**os.environ, "RUNNER_TEMP": str(work)}, stdin=subprocess.DEVNULL)
    wait_until(lambda: len(_alive(n)) == 2, timeout=20, what="the sandboxed command starting")
    proc.send_signal(15)
    assert proc.wait(timeout=20) == 128 + 15
    wait_until(lambda: not _alive(n), timeout=10, what="every sandboxed process dying")
    assert not list(work.glob("plugin-sandbox-*"))
    assert list(home.iterdir()) == []


def _kernel_overlay() -> bool:
    bwrap = shutil.which("bwrap")
    return bool(bwrap) and sandbox.overlay_mode(bwrap, "auto") == "kernel"


@needs_isolation
@pytest.mark.parametrize("mode", [
    "copy",
    pytest.param("kernel", marks=pytest.mark.skipif(not _kernel_overlay(), reason="bwrap < 0.10 has no --overlay")),
])
def test_overlay_writes_are_discarded_and_ro_paths_are_immutable(work, mode):
    home = work / "pm-home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "python3").write_text("original")
    clone = work / "clone"
    clone.mkdir()
    (clone / "plugin.yaml").write_text("name: x\n")
    script = (f"echo poisoned > {home}/bin/python3 && mkdir {home}/new && "
              f"cat {home}/bin/python3 && "
              f"if rm {clone}/plugin.yaml 2>/dev/null || touch {clone}/x 2>/dev/null; then echo CLONE_WRITABLE; fi")
    r = run("--overlay", str(home), "--overlay-mode", mode, "--ro", str(clone), "--no-network", "--", "sh", "-c", script,
            env={"RUNNER_TEMP": str(work)})
    assert r.returncode == 0, r.stderr
    assert "poisoned" in r.stdout          # writable inside
    assert "CLONE_WRITABLE" not in r.stdout
    assert (home / "bin" / "python3").read_text() == "original"
    assert not (home / "new").exists()
    assert sorted(p.name for p in clone.iterdir()) == ["plugin.yaml"]
    assert not list(work.glob("plugin-sandbox-*")), "scratch (overlay upper) left behind"


@needs_isolation
def test_no_route_to_host_loopback_or_metadata_and_proxy_refuses_them(work):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    probe = f"""
        import json, socket
        def direct(host, port):
            try:
                socket.create_connection((host, port), timeout=3).close(); return "connected"
            except OSError as exc:
                return type(exc).__name__
        def via_proxy(line):
            s = socket.create_connection(("127.0.0.1", 3128), timeout=10)
            s.sendall(line.encode() + b"\\r\\n\\r\\n")
            return s.recv(64).split(b"\\r\\n")[0].decode()
        print(json.dumps({{
            "direct_host_listener": direct("127.0.0.1", {port}),
            "direct_metadata": direct("169.254.169.254", 80),
            "proxy_host_listener": via_proxy("CONNECT 127.0.0.1:{port} HTTP/1.1"),
            "proxy_metadata": via_proxy("CONNECT 169.254.169.254:443 HTTP/1.1"),
            "proxy_localhost": via_proxy("CONNECT localhost:443 HTTP/1.1"),
            "proxy_plain_http": via_proxy("GET http://169.254.169.254/latest/meta-data/ HTTP/1.1"),
            "proxy_other_port": via_proxy("CONNECT example.com:22 HTTP/1.1"),
        }}))
    """
    try:
        r = run(*_python_mounts(), "--", *_py(probe))
    finally:
        listener.close()
    assert r.returncode == 0, r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen["direct_host_listener"] == "ConnectionRefusedError"   # the sandbox's own empty loopback
    assert seen["direct_metadata"] in ("OSError", "TimeoutError")  # ENETUNREACH: no route at all
    assert seen["proxy_host_listener"].startswith("HTTP/1.1 403")
    assert seen["proxy_metadata"].startswith("HTTP/1.1 403")
    assert seen["proxy_localhost"].startswith("HTTP/1.1 403")
    assert seen["proxy_plain_http"].startswith("HTTP/1.1 405")
    assert seen["proxy_other_port"].startswith("HTTP/1.1 403")
    assert "denied CONNECT 169.254.169.254:443" in r.stderr


@needs_isolation
@pytest.mark.skipif(not _public_https_reachable(), reason="host has no route to pypi.org:443")
def test_dependency_downloads_reach_public_https_through_the_proxy(work):
    probe = """
        import urllib.request
        with urllib.request.urlopen("https://pypi.org/simple/six/", timeout=30) as resp:
            print(resp.status, b"six-1.16.0" in resp.read())
    """
    r = run(*_python_mounts(), "--", *_py(probe))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["200", "True"], r.stdout

    # The same fetch with the proxy's host allowlist excluding pypi fails closed.
    r = run(*_python_mounts(), "--allow-host", "example.com", "--", *_py(probe))
    assert r.returncode != 0
    assert "denied CONNECT pypi.org:443 (host not allowed)" in r.stderr


# ─── the real validator through the wrapper (opt-in, needs a setup-pm home) ─────

def _tree_manifest(root: Path) -> list[tuple[str, str, int, int, int]]:
    """(path, kind, size, mtime_ns, mode) for every entry: any write shows up."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            st = p.lstat()
            kind = "l" if p.is_symlink() else "d" if p.is_dir() else "f"
            out.append((str(p.relative_to(root)), kind, st.st_size, st.st_mtime_ns, st.st_mode))
    return sorted(out)


@needs_isolation
@pytest.mark.skipif(os.environ.get("PLUGIN_SANDBOX_E2E") != "1",
                    reason="set PLUGIN_SANDBOX_E2E=1 in a setup-pm prepared shell")
def test_hermes_plugins_validate_install_deps_runs_isolated(work):
    """The catalog job's exact command: a plugin that fails import on any sign of the
    token must pass inside the wrapper, fail outside it (the control), install its
    declared dependency over the proxy, and leave the PM home unchanged."""
    hermes_home = Path(os.environ["HERMES_HOME"])
    checkout = Path(os.environ.get("PYTHONPATH") or ROOT)  # the tree `hermes` runs from
    plugin = work / "leakprobe"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "name: leakprobe\nversion: 0.1.0\ndescription: probe\npython_dependencies:\n  - cowsay==6.1\n")
    (plugin / "__init__.py").write_text(textwrap.dedent(f"""\
        import glob, os
        import cowsay  # installed by --install-deps; absent from the base environment
        def _read(p):
            try:
                return open(p, "rb").read()
            except OSError:
                return b""
        hits = [p for p in glob.glob("/proc/*/environ") if {SENTINEL!r}.encode() in _read(p)]
        if hits or {SENTINEL!r} in str(os.environ):
            raise RuntimeError("LEAK " + repr(hits))
        def register(ctx):
            pass
        """))
    env = {"GH_TOKEN": SENTINEL}
    names = ["PATH", "HERMES_HOME", "HERMES_RUNTIME_DIR", "HERMES_PYTHON", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONUTF8"]
    before = _tree_manifest(hermes_home)
    assert not [p for p in before if "cowsay" in p[0]]
    r = run("--ro", str(checkout), "--ro", str(plugin), "--overlay", str(hermes_home),
            *[a for n in names for a in ("--env", n)], "--chdir", str(checkout),
            "--", "hermes", "plugins", "validate", "--install-deps", str(plugin), env=env, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "capability probe — register() ran in isolation" in r.stdout
    # The install (and every log/state write `hermes` made) stayed in the throwaway view.
    assert _tree_manifest(hermes_home) == before

    # Control: the same leak check without the sandbox must trip, or the pass above
    # proves nothing. It runs unsandboxed, so it does write Hermes state (logs) into
    # HERMES_HOME; it has no dependency, so it installs nothing there.
    control_plugin = work / "leakprobe-control"
    shutil.copytree(plugin, control_plugin)
    init = control_plugin / "__init__.py"
    init.write_text(init.read_text().replace("import cowsay", "pass  #"))
    (control_plugin / "plugin.yaml").write_text("name: leakprobe\nversion: 0.1.0\ndescription: probe\n")
    control = subprocess.run(["hermes", "plugins", "validate", str(control_plugin)], cwd=checkout,
                             env={**os.environ, **env}, capture_output=True, text=True, timeout=600,
                             stdin=subprocess.DEVNULL, check=False)
    assert control.returncode != 0
    assert "LEAK" in control.stdout + control.stderr
