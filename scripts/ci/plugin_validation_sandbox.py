#!/usr/bin/env python3
"""Run untrusted plugin validation in a credential-free bubblewrap sandbox.

``hermes plugins validate --install-deps`` imports and runs third-party plugin
code. The catalog job's shell also holds ``GH_TOKEN`` (for ``gh attestation
verify``), and a same-user child can read a sibling's ``/proc/<pid>/environ``
or ptrace it, so dropping the variable from one subprocess's environment is no
boundary. This wrapper runs the command in fresh user, PID, mount, network,
IPC, UTS and cgroup namespaces:

* its own ``/proc``: no host process (the token-bearing shell, the runner) is
  visible, so nothing can be read from or signalled in one;
* an empty root with only ``/usr`` (+ merged-usr links), a fixed ``/etc``
  allowlist, an empty tmpfs ``$HOME`` and ``/tmp``, and the paths the caller
  names: ``--ro`` read-only, ``--overlay`` writable with the writes thrown
  away on exit (the PM home, so a plugin cannot poison the interpreter the
  host shell runs next): bwrap's overlayfs where bwrap >= 0.10 has it, else a
  per-run copy (Ubuntu 24.04's bwrap 0.9.0). Paths that are or contain credential stores, the
  runner's file-command files, ``/run`` sockets, or a checkout with persisted
  git credentials are refused;
* a cleared environment: only ``--env`` names the caller lists, and names that
  look like credentials are refused even when listed;
* no host network: only loopback. Dependency downloads leave through an HTTPS
  CONNECT proxy that this wrapper serves on a Unix socket bound into the
  sandbox. It resolves names on the host and connects only to globally
  routable addresses on allowed ports, so IMDS (169.254.169.254), loopback
  services and private ranges stay unreachable, and DNS rebinding cannot
  switch the target after the check (the checked address is the one dialled).
  IPv6 forms that carry an IPv4 address (NAT64 ``64:ff9b::/96``, IPv4-mapped
  and -compatible) are judged by that IPv4 address, and a dial that lands on
  one of the host's own addresses (src == dst) is dropped;
* ``--die-with-parent`` + PID namespace: no background child outlives the run.

Fail closed: if bwrap is missing or cannot create the namespaces, or the
process inside cannot prove its namespaces differ from the host's, the command
never runs and the wrapper exits 125 (setup) or 126 (self-check). Otherwise the
exit status is the command's.

Not covered (by design, see the report on the PR): a kernel exploit from inside
the sandbox, and anything the command prints to stdout/stderr (it reaches the
job log as before).

Usage (the catalog job)::

    python3 -I scripts/ci/plugin_validation_sandbox.py \
        --ro "$GITHUB_WORKSPACE" --ro "$CLONE_DIR" --overlay "$HERMES_HOME" \
        --env PATH --env HERMES_HOME --env HERMES_RUNTIME_DIR --env HERMES_PYTHON \
        --env VIRTUAL_ENV --env PYTHONPATH --env PYTHONUTF8 --chdir "$GITHUB_WORKSPACE" \
        -- hermes plugins validate --install-deps "$PLUGIN_DIR"

Stdlib only: it runs under ``python3 -I`` before any project code is trusted.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

EXIT_SETUP = 125
EXIT_SELFCHECK = 126

# Inside the sandbox.
INNER_DIR = "/run/hermes-sandbox"
INNER_SCRIPT = f"{INNER_DIR}/sandbox.py"
INNER_SOCKET = f"{INNER_DIR}/proxy.sock"
SANDBOX_HOME = "/home/sandbox"
PROXY_PORT = 3128

NAMESPACES = ("user", "pid", "net", "mnt", "ipc", "uts")

# Env names never forwarded, even when asked for: they are or carry credentials, or
# point at a host socket / runner file-command file.
_FORBIDDEN_ENV = re.compile(
    r"(TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE|_KEY$|^KEY$|AUTH|COOKIE|SESSION)"
    r"|^(AWS_|AZURE_|GOOGLE_|GCP_|GH_|GITHUB_|ACTIONS_|RUNNER_|INPUT_|STATE_|DOCKER_"
    r"|SSH_|GPG_|KUBECONFIG$|NETRC$|DBUS_|XDG_RUNTIME_DIR$|HERMES_TOKEN|NPM_CONFIG__AUTH)",
    re.IGNORECASE,
)

# /etc entries a Python/uv/npm toolchain needs. Never the whole of /etc: it can hold
# host credentials (gitconfig credential helpers, environment, sudoers.d, ...).
_ETC_ALLOWLIST = (
    "passwd", "group", "nsswitch.conf", "hosts", "host.conf", "gai.conf", "localtime",
    "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "alternatives", "ssl", "ca-certificates",
    "ca-certificates.conf", "pki", "crypto-policies", "mime.types", "os-release",
    "lsb-release", "debian_version", "protocols", "services",
)

# Home-relative credential stores a bound path must never be, contain, or sit inside.
_HOME_SECRETS = (
    ".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".netrc", ".git-credentials",
    ".config/gh", ".config/gcloud", ".config/hub", ".npmrc", ".pypirc", ".hermes",
    ".local/share/keyrings", ".password-store",
)

_PROXY_ENV_NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy", "npm_config_https_proxy", "npm_config_proxy")


class SetupError(Exception):
    """The sandbox cannot be built safely; the command must not run."""


# ─── host side: policy for caller-named paths ───────────────────────────────────

def _real(path: str) -> Path:
    return Path(os.path.realpath(path))


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _home_dirs(env: dict[str, str]) -> set[str]:
    """$HOME, the passwd home of this uid, and root's: the homes a bind must not expose."""
    import pwd  # Linux-only script (main() refuses other platforms); keep the import lazy

    uid = os.getuid()  # windows-footgun: ok — Linux-only script, main() refuses other platforms
    return {h for h in (env.get("HOME", ""), pwd.getpwuid(uid).pw_dir, "/root") if h}


def _kind(path: Path) -> str | None:
    """One of "dir", "file" (regular), "other", None (absent), "unknown" (cannot stat).

    Never raises: a non-root runner cannot stat inside ``/root`` (``/root/.ssh``),
    and ``Path.is_dir``/``exists`` raise PermissionError there before Python 3.14.
    Callers treat "unknown" as the unsafe answer.
    """
    try:
        mode = os.stat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "unknown"
    return "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"


def sensitive_paths(env: dict[str, str]) -> list[Path]:
    """Host locations that a bound path must not be, contain, or sit inside."""
    out: list[Path] = []
    for home in _home_dirs(env):
        out.extend(_real(os.path.join(home, rel)) for rel in _HOME_SECRETS)
    for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE",
                 "GITHUB_STEP_SUMMARY", "GITHUB_EVENT_PATH"):
        if env.get(name):
            out.append(_real(env[name]))
    # The runner's own registration credentials live at its root.
    for name in ("RUNNER_TEMP", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
        if env.get(name):
            for parent in _real(env[name]).parents:
                for marker in (".credentials", ".credentials_rsaparams", ".runner"):
                    if _kind(parent / marker) is not None:  # unknown counts as present
                        out.append(parent / marker)
    out.extend(Path(p) for p in ("/run", "/var/run", "/proc", "/sys", "/dev", "/etc",
                                 "/var/lib/docker", "/var/lib/containerd"))
    return out


def check_bind_path(path: str, env: dict[str, str]) -> Path:
    real = _real(path)
    if _kind(real) != "dir":
        raise SetupError(f"bind path is not a directory: {path}")
    if real == Path("/") or len(real.parts) < 3:
        raise SetupError(f"refusing to expose a top-level directory: {real}")
    if real in {_real(h) for h in _home_dirs(env)}:
        raise SetupError(f"refusing to expose a home directory: {real}")
    for secret in sensitive_paths(env):
        if _is_within(secret, real):
            raise SetupError(f"refusing to expose {real}: it contains {secret}")
        # Path-only, no stat: a secret this user cannot even stat still counts.
        if _is_within(real, secret):
            raise SetupError(f"refusing to expose {real}: it is inside {secret}")
    git_config = real / ".git" / "config"
    kind = _kind(git_config)
    if kind is not None:
        try:
            if kind != "file":
                raise OSError(f"not a readable regular file ({kind})")
            text = git_config.read_text(encoding="utf-8-sig", errors="replace").lower()
        except OSError as exc:
            raise SetupError(f"refusing to expose {real}: cannot vet its .git/config: {exc}") from exc
        if "extraheader" in text or "authorization" in text or re.search(r"https://[^/\s]+:[^/\s]+@", text):
            raise SetupError(f"refusing to expose {real}: its .git/config carries credentials")
    return real


def check_env_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SetupError(f"invalid env name: {name!r}")
    if _FORBIDDEN_ENV.search(name) or name in _PROXY_ENV_NAMES:
        raise SetupError(f"refusing to forward env {name}: it may carry a credential or host endpoint")


# ─── host side: the egress proxy ────────────────────────────────────────────────

def _log(message: str) -> None:
    # One line, printable only: the target text is plugin-controlled and the log is
    # parsed for workflow commands.
    safe = "".join(c if c.isprintable() else "?" for c in message)[:300]
    print(f"plugin-sandbox: {safe}", file=sys.stderr, flush=True)


def _relay(a: socket.socket, b: socket.socket, idle: float) -> None:
    socks = [a, b]
    try:
        while True:
            ready, _, _ = select.select(socks, [], [], idle)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


# IPv6 prefixes whose low 32 bits are an IPv4 destination: NAT64 (RFC 6052 well-known
# prefix; a VPC with DNS64/NAT64 routes it to a NAT gateway that dials the IPv4) and the
# deprecated IPv4-compatible form. ``ipaddress`` calls both global whatever IPv4 they carry.
_IPV4_EMBEDDING = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("::/96"))


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _IPV4_EMBEDDING):
            if not _public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)):
                return False
    return ip.is_global and not ip.is_multicast


def _is_self_connection(sock: socket.socket) -> bool:
    """True when the dial landed on one of this host's own addresses.

    Linux routes a connection to a local address through ``lo`` with that same
    address as the source, so src == dst. A runner whose public (or global IPv6)
    address sits on its interface would otherwise expose its own listeners.
    """
    try:
        local, peer = sock.getsockname()[0], sock.getpeername()[0]
    except OSError:
        return True
    return ipaddress.ip_address(str(local).split("%")[0]) == ipaddress.ip_address(str(peer).split("%")[0])


def _dial(addresses: list[tuple[str, int]]) -> socket.socket | str:
    """Connect to the first reachable checked address (never a re-resolve), or say why not."""
    for address in addresses:
        try:
            upstream = socket.create_connection(address, timeout=15)
        except OSError:
            continue
        if _is_self_connection(upstream):
            upstream.close()
            return f"{address[0]} is this host"
        return upstream
    return "unreachable"


class EgressPolicy:
    def __init__(self, ports: set[int], hosts: list[str]):
        self.ports = ports
        self.hosts = [h.lower().lstrip(".") for h in hosts]

    def host_allowed(self, host: str) -> bool:
        if not self.hosts:
            return True
        host = host.lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def resolve(self, host: str, port: int) -> list[tuple[str, int]] | str:
        """The checked addresses to dial (in resolver order), or a denial reason."""
        if port not in self.ports:
            return f"port {port} not allowed"
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}|\[[0-9A-Fa-f:.]+\]", host):
            return "malformed host"
        host = host.strip("[]")
        if not self.host_allowed(host):
            return "host not allowed"
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            return f"resolve failed: {exc.__class__.__name__}"
        addrs = []
        for _family, _t, _p, _c, sockaddr in infos:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
            # Any internal answer denies the name: a public name resolving to an
            # internal address is exactly how IMDS/loopback would be reached.
            if not _public(ip):
                return f"resolves to non-public address {ip}"
            if (str(ip), port) not in addrs:
                addrs.append((str(ip), port))
        if not addrs:
            return "no address"
        return addrs


class EgressProxy:
    """CONNECT-only proxy on a Unix socket; one thread per tunnel."""

    MAX_TUNNELS = 64
    HEAD_LIMIT = 8192

    def __init__(self, path: str, policy: EgressPolicy, idle: float = 300.0):
        self.policy = policy
        self.idle = idle
        self.slots = threading.BoundedSemaphore(self.MAX_TUNNELS)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o600)
        self.sock.listen(64)
        self.closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self.closed:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(1024)
                if not chunk:
                    return
                head += chunk
                if len(head) > self.HEAD_LIMIT:
                    client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                    return
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            m = re.fullmatch(r"CONNECT (\S+):(\d{1,5}) HTTP/1\.[01]", line)
            if not m:
                _log(f"denied non-CONNECT request {line[:120]!r}")
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            host, port = m.group(1), int(m.group(2))
            target = self.policy.resolve(host, port)
            if isinstance(target, str):
                _log(f"denied CONNECT {host}:{port} ({target})")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            upstream = _dial(target)
            if isinstance(upstream, str):
                _log(f"denied CONNECT {host}:{port} ({upstream})")
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                upstream.sendall(rest)
            client.settimeout(None)
            upstream.settimeout(None)
            _relay(client, upstream, self.idle)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            self.slots.release()


# ─── host side: build and run bwrap ─────────────────────────────────────────────

def host_namespaces() -> dict[str, str]:
    out = {}
    for ns in NAMESPACES:
        try:
            out[ns] = os.readlink(f"/proc/self/ns/{ns}")
        except OSError as exc:
            raise SetupError(f"cannot read host {ns} namespace: {exc}") from exc
    return out


def _inner_python() -> tuple[str, list[str]]:
    """An interpreter for the in-sandbox init, plus any extra ro mount it needs."""
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(candidate, os.X_OK):
            return candidate, []
    exe = os.path.realpath(sys.executable)
    prefix = os.path.realpath(sys.base_prefix)
    return exe, [prefix]


def overlay_mode(bwrap: str, requested: str) -> str:
    """kernel: bwrap's own overlayfs (>= 0.10); copy: a per-run snapshot."""
    if requested != "auto":
        return requested
    try:
        text = subprocess.run([bwrap, "--help"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"cannot run {bwrap}: {exc}") from exc
    return "kernel" if "--overlay-src" in text else "copy"


def _system_mounts() -> list[str]:
    """The fixed host view: merged-usr links, /usr, an /etc allowlist, private tmpfs dirs."""
    argv: list[str] = []
    for top in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        host = Path("/") / top
        if host.is_symlink():
            argv += ["--symlink", os.readlink(host), f"/{top}"]
        elif host.is_dir():
            argv += ["--ro-bind", str(host), f"/{top}"]
    argv += ["--ro-bind", "/usr", "/usr"]
    for name in _ETC_ALLOWLIST:
        argv += ["--ro-bind-try", f"/etc/{name}", f"/etc/{name}"]
    # no-tmp: ok — these are the sandbox's own empty tmpfs mounts, not host scratch paths.
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm", "--tmpfs", "/tmp",
             "--tmpfs", "/run", "--dir", INNER_DIR, "--tmpfs", "/home", "--dir", SANDBOX_HOME,
             "--tmpfs", "/var", "--dir", "/var/tmp"]
    return argv


def _overlay_mounts(bwrap: str, requested_mode: str, overlays: list[Path], scratch: Path) -> list[str]:
    """Writable views of ``overlays`` whose writes never reach the host paths."""
    argv: list[str] = []
    mode = overlay_mode(bwrap, requested_mode) if overlays else "none"
    for i, path in enumerate(overlays):
        if mode == "kernel":
            upper, work = scratch / f"overlay{i}" / "upper", scratch / f"overlay{i}" / "work"
            upper.mkdir(parents=True)
            work.mkdir(parents=True)
            argv += ["--overlay-src", str(path), "--overlay", str(upper), str(work), str(path)]
            continue
        # bwrap < 0.10 (Ubuntu 24.04 ships 0.9.0) has no --overlay: give the sandbox a
        # private copy at the same path instead. Same contract, paid in disk and time.
        copy = scratch / f"copy{i}" / path.name
        copy.parent.mkdir(parents=True)
        try:
            r = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(path), str(copy)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=600, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SetupError(f"snapshot of {path} timed out") from exc
        if r.returncode != 0:
            raise SetupError(f"cannot snapshot {path}: {r.stderr.strip()[-300:]}")
        argv += ["--bind", str(copy), str(path)]
    return argv


def _inner_env(args, env: dict[str, str]) -> dict[str, str]:
    """The sandbox's whole environment: fixed basics, vetted forwards, the proxy."""
    # no-tmp: ok — TMPDIR names the sandbox's private tmpfs, not a host path.
    inner = {"HOME": SANDBOX_HOME, "TMPDIR": "/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
             "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin",
             "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    for name in args.env:
        check_env_name(name)
        if name in env:
            inner[name] = env[name]
    for item in args.setenv:
        name, sep, value = item.partition("=")
        if not sep:
            raise SetupError(f"--setenv needs NAME=VALUE: {item!r}")
        check_env_name(name)
        inner[name] = value
    if not args.no_network:
        url = f"http://127.0.0.1:{PROXY_PORT}"
        inner.update({n: url for n in _PROXY_ENV_NAMES})
        inner.update({"NO_PROXY": "", "no_proxy": ""})
    return inner


def bwrap_argv(args, scratch: Path, env: dict[str, str]) -> list[str]:
    bwrap = args.bwrap or shutil.which("bwrap")
    if not bwrap:
        raise SetupError("bubblewrap (bwrap) is not installed")
    ro = [check_bind_path(p, env) for p in args.ro]
    overlays = [check_bind_path(p, env) for p in args.overlay]
    chdir = _real(args.chdir) if args.chdir else Path(SANDBOX_HOME)
    if args.chdir and not any(_is_within(chdir, p) for p in ro + overlays):
        raise SetupError(f"--chdir {chdir} is not inside a bound path")

    argv = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--new-session", "--cap-drop", "ALL", "--clearenv", "--hostname", "plugin-sandbox"]
    if os.getuid() == 0:  # windows-footgun: ok — Linux-only script, main() refuses other platforms
        # Never hand plugin code uid 0, not even a namespaced one.
        argv += ["--uid", "65534", "--gid", "65534"]
    argv += _system_mounts()
    python, extra_ro = _inner_python()
    for path in [*extra_ro, *map(str, ro)]:
        argv += ["--ro-bind", path, path]
    argv += _overlay_mounts(bwrap, args.overlay_mode, overlays, scratch)
    argv += ["--ro-bind", os.path.realpath(__file__), INNER_SCRIPT]
    if not args.no_network:
        argv += ["--bind", str(scratch / "proxy.sock"), INNER_SOCKET]
    for name, value in _inner_env(args, env).items():
        argv += ["--setenv", name, value]
    argv += ["--chdir", str(chdir), "--", python, "-I", "-S", INNER_SCRIPT, "--inner"]
    argv += [f"--host-ns={ns}:{ident}" for ns, ident in host_namespaces().items()]
    if args.no_network:
        argv += ["--no-network"]
    return argv + ["--", *args.command]


def remove_tree(path: Path) -> None:
    """Delete the scratch dir even where the sandbox left it unlistable (chmod 000 dirs).

    Every dir in it is ours, so chmod works on it whatever its mode: open each one
    up top-down (never through a symlink), then remove the tree.
    """
    pending = [str(path)]
    while pending:
        directory = pending.pop()
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                continue
            os.chmod(directory, stat.S_IRWXU)
            with os.scandir(directory) as entries:
                pending.extend(e.path for e in entries if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)


def run_host(args) -> int:
    env = dict(os.environ)
    if not args.command:
        print("plugin-sandbox: no command given", file=sys.stderr)
        return EXIT_SETUP
    try:
        # Refuse bad requests before creating anything on the host.
        for path in [*args.ro, *args.overlay]:
            check_bind_path(path, env)
        for name in args.env:
            check_env_name(name)
    except SetupError as exc:
        print(f"plugin-sandbox: {exc}", file=sys.stderr)
        return EXIT_SETUP
    base = env.get("RUNNER_TEMP", "")
    if not os.path.isdir(base):
        base = tempfile.gettempdir()
    scratch = Path(tempfile.mkdtemp(prefix="plugin-sandbox-", dir=base))
    proxy = None
    proc = None

    def _terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        try:
            argv = bwrap_argv(args, scratch, env)
            if not args.no_network:
                policy = EgressPolicy(set(args.allow_port or [443]), args.allow_host)
                proxy = EgressProxy(str(scratch / "proxy.sock"), policy)
        except (SetupError, OSError) as exc:
            print(f"plugin-sandbox: {exc}", file=sys.stderr)
            return EXIT_SETUP
        # bwrap itself gets no inherited secrets either (it does not need any).
        # bwrap's own setup failures exit 1 like a failing command; either way the
        # caller sees a nonzero status and the entry fails.
        proc = subprocess.Popen(argv, env={"PATH": "/usr/bin:/bin"}, close_fds=True)
        # health: allow HX006 -- this wait IS the sandboxed command's runtime; the caller bounds it (the job's `timeout 600`) and SIGTERM/SIGINT tear the namespace down below
        return proc.wait()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()  # bwrap's PID namespace (every sandboxed process) dies with it
            try:
                proc.wait(timeout=30)  # reaped before its scratch (the overlay copy) is removed
            except subprocess.TimeoutExpired:
                pass
        if proxy:
            proxy.close()
        remove_tree(scratch)


# ─── inside the sandbox ─────────────────────────────────────────────────────────

def _forward(listener: socket.socket) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(INNER_SOCKET)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_relay, args=(client, upstream, 300.0), daemon=True).start()


def self_check(host_ns: dict[str, str]) -> str | None:
    for ns in NAMESPACES:
        try:
            mine = os.readlink(f"/proc/self/ns/{ns}")
        except OSError as exc:
            return f"cannot read {ns} namespace: {exc}"
        if ns not in host_ns:
            return f"host {ns} namespace not provided"
        if mine == host_ns[ns]:
            return f"{ns} namespace is shared with the host"
    leaked = sorted(n for n in os.environ if _FORBIDDEN_ENV.search(n) and n not in _PROXY_ENV_NAMES)
    if leaked:
        return f"credential-like env reached the sandbox: {', '.join(leaked)}"
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    if len(pids) > 8:
        return f"{len(pids)} processes visible: /proc is not private"
    return None


def run_inner(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="plugin-sandbox --inner")
    parser.add_argument("--inner", action="store_true")
    parser.add_argument("--host-ns", action="append", default=[])
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    host_ns = dict(item.split(":", 1) for item in args.host_ns)
    problem = self_check(host_ns)
    if problem:
        print(f"plugin-sandbox: self-check failed, not running the command: {problem}",
              file=sys.stderr, flush=True)
        return EXIT_SELFCHECK
    if not args.no_network:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", PROXY_PORT))
        listener.listen(64)
        threading.Thread(target=_forward, args=(listener,), daemon=True).start()
    try:
        child = subprocess.Popen(command)
    except OSError as exc:
        print(f"plugin-sandbox: cannot start {command[:1]}: {exc}", file=sys.stderr)
        return 127
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    # health: allow HX006 -- the sandboxed command's runtime; bwrap dies with the host wrapper, which the caller bounds
    return child.wait()


# ─── entry ──────────────────────────────────────────────────────────────────────

def parse_host_args(argv: list[str]):
    parser = argparse.ArgumentParser(description="Run a command in a credential-free bubblewrap sandbox.")
    parser.add_argument("--ro", action="append", default=[], metavar="DIR",
                        help="expose DIR read-only at the same path")
    parser.add_argument("--overlay", action="append", default=[], metavar="DIR",
                        help="expose DIR writable at the same path; writes are discarded on exit")
    parser.add_argument("--env", action="append", default=[], metavar="NAME",
                        help="forward this host env var (credential-like names are refused)")
    parser.add_argument("--setenv", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--chdir", metavar="DIR", help="working directory (inside a bound path)")
    parser.add_argument("--allow-port", action="append", type=int, metavar="PORT",
                        help="egress port the proxy may dial (default: 443 only)")
    parser.add_argument("--allow-host", action="append", default=[], metavar="DOMAIN",
                        help="restrict egress to these domains and their subdomains (default: any public host)")
    parser.add_argument("--no-network", action="store_true", help="loopback only, no proxy")
    parser.add_argument("--overlay-mode", choices=("auto", "kernel", "copy"), default="auto",
                        help="how --overlay is provided (default: kernel overlayfs when bwrap supports it)")
    parser.add_argument("--bwrap", help=argparse.SUPPRESS)  # tests: a broken/fake bwrap
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--inner"]:
        return run_inner(argv)
    if sys.platform != "linux":
        print("plugin-sandbox: Linux only", file=sys.stderr)
        return EXIT_SETUP
    return run_host(parse_host_args(argv))


if __name__ == "__main__":
    sys.exit(main())
