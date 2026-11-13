"""Observational executed-code receipts, not a sandbox or a coverage approval.

Install at the reviewed bootstrap boundary. Direct construction supports isolated
fixtures but never establishes bootstrap provenance. Source manifests are review
inputs, not permission to declare arbitrary code passive. No third-party imports.
"""
import ast
import base64
import csv
import dis
import _imp
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
from collections import OrderedDict
from itertools import islice
import threading
from types import CodeType, FunctionType


# Required in every supported route (missing => unknown). Blob-compared, never audited.
SEED_COMMON = {
    "hermes_bootstrap": "hermes_bootstrap.py",
    "hermes_cli": "hermes_cli/__init__.py",
    "hermes_cli._early_recovery": "hermes_cli/_early_recovery.py",
    "hermes_cli.git_fingerprint": "hermes_cli/git_fingerprint.py",
    "hermes_cli.code_attestation": "hermes_cli/code_attestation.py",
    "hermes_cli.local_work": "hermes_cli/local_work.py",
    "hermes_cli._launch_template": "hermes_cli/_launch_template.py",
}
SEED_MAIN = {"hermes_cli.main": "hermes_cli/main.py"}
# The closed ALLOWED inventory (common plus main). Which of these is REQUIRED before the
# hook depends on the route install() derives itself: main is seed only in the
# console-main-first route and an audited post-hook unit in the managed-stub route.
SEED = {**SEED_COMMON, **SEED_MAIN}
ROUTE_MANAGED_STUB = "managed-stub"
ROUTE_CONSOLE = "console-main-first"
# Reviewed launcher-head digests (sha256 of the script before the pin marker with the
# checkout-root literal replaced by ``@ROOT@``). Anything else => unsupported-stub-head.
# Reproduce: split a stub's script at ``sys._hermes_pin_default_home = True\n``, replace
# ``repr(root)`` with ``@ROOT@`` in the head, sha256. The first is the current template
# (``_launch_template.render``, also recomputed from the loaded template at install);
# the second is the stub backed up in hermes-state ``.hermes/bin/hermes`` (Oct 22),
# whose tail is byte-identical to the current one.
CURRENT_TEMPLATE_HEAD_DIGEST = "0c20fb93df616f742327551a88ff13d45a66ddd08da54e507d7694b59f387b17"
BACKUP_STUB_HEAD_DIGEST = "cb995ab282afb089332832324bef53237f78cf2801070ff86c569c1b715c0be9"
STUB_HEAD_DIGESTS = frozenset({CURRENT_TEMPLATE_HEAD_DIGEST, BACKUP_STUB_HEAD_DIGEST})
LAUNCH_HANDOFF_ENV = "HERMES_LAUNCH_CURRENT_HANDOFF"  # venv_sync.LAUNCH_HANDOFF_ENV (not importable here)
_ROUTE_SENTINEL_ROOT = "/@attest-root-sentinel@"
MAX_SOURCE = 2 * 1024 * 1024
MAX_TREE = 64 * 1024 * 1024
MAX_UNITS = 16384
MAX_LOADED_CODE = 1 << 18  # code objects kept for executed-code lookups; the gateway graph uses ~70k
MAX_CACHE_ENTRIES = 32
MAX_CACHE_BYTES = 8 * 1024 * 1024
_FROZEN_ALIASES: dict[str, str] = {"importlib._bootstrap": "_frozen_importlib",
                   "importlib._bootstrap_external": "_frozen_importlib_external"}


MAX_DISTS = 4096
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_DEP_REPORT = 4096
DEPENDENCY_TRUST = "installer-receipt: trusted, not audited, no coverage"
FORWARDREF_TRUST = (
    "R5-2 interpreter trust class: exactly this annotation expression text, evaluated by the "
    "stdlib annotationlib.ForwardRef.evaluate, called by attested code (first attested caller "
    "outward, which may be a dependency or core frame). The text is owned by whatever object "
    "carried the annotation; no coverage and no <string>-wide or package-wide exemption.")
# R5-1: the only dependency producers (canonical PEP 503 dist name, RECORD-relative path) whose
# exec/eval call sites are receipted. Descriptors derive from the receipted module bytes.
_DEPENDENCY_GENERATORS = {
    ("typing-inspection", "typing_inspection/typing_objects.py"):
        ("_compile_identity_check_function", "_compile_isinstance_check_function"),
}
# R4-D: the only process-image/system sonames a ctypes caller may load (measured).
_NATIVE_SONAMES = (None, "libc.so.6")


class _DependencyIndex:
    """Installer RECORD receipts for the managed environment's site-packages (R4).

    Dependency bytes join the interpreter trust class by receipt, are never audited
    units and never coverage. Roots are only ``sys.path`` entries named
    ``site-packages`` under ``sys.prefix``; rows granting nothing (non-sha256, ``..``,
    absolute) are dropped. Built lazily once; overflow is reported, never partial.
    """

    def __init__(self, prefix, paths):
        prefix = Path(prefix).resolve()
        self.roots = ()
        self.rows = {}
        self.dists = {}
        self.limit = False
        self._built = set()
        self._total = 0
        for entry in paths:
            try:
                root = Path(entry or ".").resolve()
            except (OSError, ValueError):
                continue
            if root.is_relative_to(prefix):
                self.add_root(root)

    def add_root(self, root):
        """Only a directory named site-packages; never PYTHONPATH/user site/cwd by name alone."""
        if root.name == "site-packages" and root.is_dir() and root not in self.roots:
            self.roots = (*self.roots, root)
            return True
        return False

    def _parse(self, root, info, text):
        dist = info.name[:-len(".dist-info")]
        for row in csv.reader(text.splitlines()):
            if len(row) < 3 or not row[1].startswith("sha256="):
                continue
            relative = Path(row[0])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            try:
                digest = base64.urlsafe_b64decode(row[1][7:] + "=" * (-len(row[1][7:]) % 4)).hex()
                size = int(row[2])
            except ValueError:
                continue
            self.rows[root / relative] = (dist, digest, size)

    def build(self):
        for root in self.roots:
            if root in self._built or self.limit:
                continue
            self._built.add(root)
            infos = sorted(root.glob("*.dist-info"))
            if len(self.dists) + len(infos) > MAX_DISTS:
                self.limit = True
                return
            for info in infos:
                try:
                    record = _read_bounded(info / "RECORD", MAX_RECORD_BYTES - self._total)
                except (OSError, ValueError):
                    if (info / "RECORD").exists():
                        self.limit = True
                        return
                    continue  # no RECORD: its files stay unreceipted
                self._total += len(record)
                try:
                    installer = _sha(_read_bounded(info / "INSTALLER", 4096))
                except (OSError, ValueError):
                    installer = None
                self.dists[info.name[:-len(".dist-info")]] = {
                    "dir": str(root), "record_sha256": _sha(record),
                    "installer_sha256": installer, "used": False}
                self._parse(root, info, record.decode("utf-8", "replace"))

    def indexed(self, path):
        return any(path.is_relative_to(root) for root in self.roots)

    def identity(self, path):
        """(canonical PEP 503 dist name, version, relative posix path) of a RECORD row, else None."""
        row = self.rows.get(path)
        if row is None:
            return None
        stem = row[0]
        name, _, version = stem.rpartition("-")
        relative = next((path.relative_to(root).as_posix() for root in self.roots
                         if path.is_relative_to(root)), None)
        if not name or relative is None:
            return None
        return re.sub(r"[-_.]+", "-", name).lower(), version, relative

    def verify(self, path, data=None):
        """(dist, digest) when the file's current bytes match its receipt, else None."""
        self.build()
        row = self.rows.get(path)
        if row is None:
            return None
        dist, digest, size = row
        try:
            if data is None:
                with path.open("rb") as stream:
                    data_digest = hashlib.file_digest(stream, "sha256").hexdigest()
                actual = path.stat().st_size
            else:
                data_digest, actual = _sha(data), len(data)
        except OSError:
            return None
        if data_digest != digest or actual != size:
            return None
        self.dists[dist]["used"] = True
        return dist, digest


def _read_bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("source-limit")
    return data


def _binary_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _retained_size(value):
    # Account for compiled constants as well as instructions, including nested
    # tuples/frozensets. Overcount sharing conservatively rather than trust mtime.
    size = sys.getsizeof(value)
    if isinstance(value, CodeType):
        return size + sum(_retained_size(item) for item in (
            value.co_consts, value.co_code, value.co_linetable,
            value.co_exceptiontable, value.co_names, value.co_varnames,
            value.co_freevars, value.co_cellvars, value.co_filename,
            value.co_name, value.co_qualname))
    if isinstance(value, (tuple, frozenset)):
        size += sum(_retained_size(item) for item in value)
    return size


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _codes(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _codes(value)


def _same(actual, expected):
    # CodeType equality includes bytecode, constants, exception/line tables and
    # signature but omits filename; require that recursively as well.
    return (actual == expected and
            tuple(c.co_filename for c in _codes(actual)) ==
            tuple(c.co_filename for c in _codes(expected)))


def _compile(source, filename, mode="exec"):
    return compile(source, filename, mode, flags=0, dont_inherit=True,
                   optimize=sys.flags.optimize)


def _blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _boot_blobs(root, sha):
    if re.fullmatch(r"[0-9a-f]{40}", sha or "") is None:
        raise ValueError("missing-boot-sha")
    # One finite boot-pinned tree read. Verify disk bytes by Git blob ID before
    # compiling them: equal bytes are the pinned source, without a 140MB archive.
    argv = ["git", "-C", str(root), "ls-tree", "-rz", sha]
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env={"PATH": os.defpath, "GIT_NO_LAZY_FETCH": "1",
                               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}) as process:
        # A bounded pipe read, not communicate/capture_output followed by a size
        # check. The watchdog also covers a child that never closes its pipe.
        timer = threading.Timer(30, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            output = process.stdout.read(MAX_TREE + 1)
            if len(output) > MAX_TREE:
                process.kill()
                raise ValueError("boot-tree-limit")
            if process.wait(timeout=30) != 0:
                raise ValueError("boot-tree-read-failed")
        except BaseException:
            process.kill()
            process.wait(timeout=30)
            raise
        finally:
            timer.cancel()
            timer.join()
    blobs = {}
    for row in output.split(b"\0"):
        if not row:
            continue
        metadata, filename = row.split(b"\t", 1)
        mode, kind, digest = metadata.split()
        if kind == b"blob" and mode in (b"100644", b"100755") and filename.endswith(b".py"):
            blobs[os.fsdecode(filename)] = digest.decode("ascii")
    return blobs


class GeneratorDescriptor:
    """Exact installed-stdlib producer code and exec/eval instruction positions."""

    def __init__(self, name, function, path, distribution, *, kind="compile", trust=None):
        source = _read_bounded(path, MAX_SOURCE)
        expected = next(code for code in _codes(_compile(source, str(path)))
                        if code.co_qualname == function.__code__.co_qualname)
        if not _same(function.__code__, expected):
            raise ValueError("generator-loaded-code-skew:" + name)
        self._init(name, function.__code__, source, distribution, kind, trust)

    @classmethod
    def from_executed(cls, name, module_code, qualname, source, distribution):
        """R5-1/A1: build from the receipted bytes and the executed module code, never a re-read."""
        code = next((c for c in _codes(module_code) if c.co_qualname == qualname), None)
        if code is None:
            raise ValueError("generator-callsite-missing:" + name)
        descriptor = cls.__new__(cls)
        descriptor._init(name, code, source, distribution, "compile", None)
        return descriptor

    def _init(self, name, code, source, distribution, kind, trust):
        self.name = name
        self.code = code
        self.kind = kind  # "compile": compile receipt paired with exec; "forwardref": exec-time check
        self.trust = trust
        self.source_sha256 = _sha(source)
        self.distribution = distribution
        self.sites = {}
        instructions = tuple(dis.get_instructions(self.code, show_caches=True))
        self.instruction_sites = {}
        current = -1
        for instruction in instructions:
            if instruction.opname != "CACHE":
                current = instruction.offset
            self.instruction_sites[instruction.offset] = current
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("exec", "eval")):
                continue
            position = (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)
            for instruction in instructions:
                if (instruction.opname in ("CALL", "CALL_KW") and instruction.positions is not None
                        and tuple(instruction.positions) == position):
                    self.sites[instruction.offset] = node.func.id
        want = 2 if kind == "forwardref" else 1
        if len(self.sites) < want:
            raise ValueError("generator-callsite-missing:" + name)


class _Refuse(Exception):
    """Route identity could not be established: the reason becomes a sticky unknown."""


def _template_parts(template):
    # The loaded (and later blob-compared) stdlib-only template; module/func are fixed.
    script = template.render(_ROUTE_SENTINEL_ROOT, "hermes_cli.main", "main")
    parts = template.split_pin_marker(script)
    if parts is None:
        raise _Refuse("unsupported-route:template-marker")
    head, tail = parts
    return _sha(template.normalized_head(head, _ROUTE_SENTINEL_ROOT).encode()), tail


def _caller_frames(own_file):
    frame = sys._getframe()
    while frame is not None and frame.f_code.co_filename == own_file:
        frame = frame.f_back
    return frame


def _detect_route(core_root, template, own_file):
    """Derive the launch route from evidence only: argv, the __main__ frame, template.

    Nothing the caller passes (or any ``sys`` flag such as the pin marker) participates.
    Returns a dict for the supported managed-stub route; raises ``_Refuse`` otherwise.
    """
    orig = list(getattr(sys, "orig_argv", ()))
    argv = list(sys.argv)
    if getattr(sys, "_hermes_relaunched", False) or LAUNCH_HANDOFF_ENV in os.environ:
        raise _Refuse("relaunched-launch")
    if len(orig) < 4 or orig[1:3] != ["-I", "-c"]:
        main_module = sys.modules.get("__main__")
        if getattr(getattr(main_module, "__spec__", None), "name", None) or "-m" in orig[1:3]:
            raise _Refuse("unsupported-route:python-m")
        if "hermes_cli.main" in sys.modules:
            # Console script with main executing: the A1 route needs finite exact
            # console-stub normalization, which this build does not implement.
            raise _Refuse("unsupported-route:" + ROUTE_CONSOLE)
        raise _Refuse("unsupported-route:other")
    script = orig[3]
    if argv[:1] != ["-c"] or argv[1:] != orig[4:]:
        raise _Refuse("unsupported-route:argv-mismatch")
    if argv[1:2] == ["--run-module"]:
        raise _Refuse("unsupported-route:run-module")
    if argv[1:2] == ["--print-runtime-command"]:
        raise _Refuse("unsupported-route:print-runtime-command")
    if argv[1:2] == ["update"] and "--post-swap" in argv[2:]:
        raise _Refuse("unsupported-route:post-swap")
    if not isinstance(script, str):
        raise _Refuse("unsupported-route:script")
    parts = template.split_pin_marker(script)
    if parts is None:
        raise _Refuse("unsupported-route:no-pin-marker")
    head, tail = parts
    current_digest, template_tail = _template_parts(template)
    if tail != template_tail:
        raise _Refuse("unsupported-route:stub-tail")
    root = str(core_root)
    if repr(root) not in head:
        raise _Refuse("unsupported-stub-root")
    head_digest = _sha(template.normalized_head(head, root).encode())
    if head_digest not in STUB_HEAD_DIGESTS | {current_digest}:
        raise _Refuse("unsupported-stub-head")
    if "hermes_cli.main" in sys.modules:
        raise _Refuse("unexpected-prehook:hermes_cli.main")
    frame = _verify_frame_chain(core_root, own_file, script, head)
    return {"route": ROUTE_MANAGED_STUB, "stub_head_digest": head_digest,
            "head_sha256": head_digest, "tail_sha256": _sha(tail.encode()),
            "script_sha256": _sha(script.encode()), "launcher_argv0": orig[0],
            "current_template_head_digest": current_digest, "code": frame.f_code}


def _verify_frame_chain(core_root, own_file, script, head):
    """Return the outermost ``<string>`` frame proven to be ``script``; raise ``_Refuse`` otherwise."""
    # Frame chain: install <- (one bootstrap helper) <- hermes_bootstrap module frame <-
    # importlib frames only <- the outermost ``<string>`` frame that is this script.
    frame = _caller_frames(own_file)
    boot_file = str(core_root / "hermes_bootstrap.py")
    if (frame is not None and frame.f_code.co_name == "_install_code_attestation"
            and frame.f_globals.get("__name__") == "hermes_bootstrap"):
        frame = frame.f_back
    if (frame is None or frame.f_globals.get("__name__") != "hermes_bootstrap"
            or frame.f_code.co_name != "<module>" or frame.f_code.co_filename != boot_file):
        raise _Refuse("unsupported-route:frame-chain")
    frame = frame.f_back
    while frame is not None and frame.f_back is not None:
        if (not frame.f_code.co_filename.startswith("<frozen importlib._bootstrap")
                or not frame.f_globals.get("__name__", "").startswith("importlib._bootstrap")):
            raise _Refuse("unsupported-route:frame-chain")
        frame = frame.f_back
    if (frame is None or frame.f_back is not None or frame.f_code.co_filename != "<string>"
            or frame.f_code.co_name != "<module>" or frame.f_globals.get("__name__") != "__main__"):
        raise _Refuse("unsupported-route:frame-chain")
    if not _same(frame.f_code, _compile(script, "<string>")):
        raise _Refuse("unsupported-route:frame-code")
    marker_line = head.count("\n") + 1  # the pin marker line; try: and import follow
    lines = script.splitlines()
    if (frame.f_lineno != marker_line + 2 or lines[frame.f_lineno - 1].strip() != "import hermes_bootstrap"
            or lines[frame.f_lineno - 2].strip() != "try:"):
        raise _Refuse("unsupported-route:frame-line")
    return frame


_INSTALL_KEY = object()  # private: only install() constructs a route-deriving Attestation


class Attestation:
    """Process-local sticky ledger. No API ever sets coverage_complete=True."""

    def __init__(self, core_root, boot_sha, *, _install_key=None):
        self.core_root = Path(core_root).resolve()
        self.boot_sha = boot_sha
        self.route = None
        self.stub_head_digest = None
        self._launcher = None
        self._refused = False
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._unknown = {"bootstrap-provenance-pending"}
        self._generation = 0
        self._units = []
        self._known = {}  # identity-keyed strong references: no id recycling
        self._components = {}
        self._seed = []
        self._generators = {}
        self._forwardref = None
        self._dep_generators = {}  # (dist, relative path) -> ids of its live descriptors (A4)
        self._fwd_seen = set()
        self._source_cache = OrderedDict()
        self._cache_bytes = 0
        self._modules = {}  # bounded strong identity inventory, including replacements
        self._stdlib = Path(sysconfig.get_path("stdlib")).resolve()
        self._blobs = {}
        # Route detection runs first: a refused route latches unsupported-route without paying for
        # the interpreter hash or the dependency root scan (an empty index keeps reports total).
        self._deps = _DependencyIndex(sys.prefix, ())
        self._dep_units = {}  # executed/loaded receipts only, never audited units
        self._native_loads = []
        self._unknown_detail = {}
        self.interpreter_identity = {
            "executable": str(Path(sys.executable).resolve()),
            "version": sys.version, "cache_tag": sys.implementation.cache_tag,
            "prefix": sys.prefix, "base_prefix": sys.base_prefix,
            "binary_sha256": None,
        }
        route = None
        required = SEED  # direct construction: the full allowed inventory, still pending
        if _install_key is _INSTALL_KEY:
            import hermes_cli._launch_template as template  # unconditional, never route-lazy
            try:
                route = _detect_route(self.core_root, template, __file__)
            except _Refuse as refusal:
                # Unsupported/unknown launch: sticky unknown, no hook, no boot-tree read
                # (nothing could ever clear it); reported through unknowns().
                self._unknown.add(str(refusal))
                self._refused = True
                return
            self.route = route["route"]
            self.stub_head_digest = route["stub_head_digest"]
            required = SEED_COMMON
        self._deps = _DependencyIndex(sys.prefix, sys.path)
        self.interpreter_identity["binary_sha256"] = _binary_sha(Path(sys.executable))
        try:
            self._blobs = _boot_blobs(self.core_root, boot_sha)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.mark_unknown("boot-tree-unavailable:" + type(exc).__name__)
        self._components["core"] = (self.core_root, "core", {})
        self._inventory_preexisting(required)
        if route is not None:
            # The executing launcher script is trusted by identity (head digest in the
            # reviewed set + byte-exact tail), reported as seed, never as audited, and
            # never in the blob list. Interpreter identity is recorded separately.
            self._launcher = {
                "kind": "trusted_launcher_script", "route": route["route"],
                "head_sha256": route["head_sha256"], "tail_sha256": route["tail_sha256"],
                "script_sha256": route["script_sha256"],
                "orig_argv0": route["launcher_argv0"],
                "interpreter_binary_sha256": self.interpreter_identity["binary_sha256"]}
            self._seed.append(dict(self._launcher))
        # Reviewed stdlib generator descriptors must exist before the hook sees the
        # first post-hook namedtuple/dataclass generation (e.g. concurrent.futures).
        self.install_stdlib_generators()
        sys.addaudithook(self._audit)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)
        if route is not None and self._unknown == {"bootstrap-provenance-pending"}:
            # The ONLY path that clears provenance: install()'s own, after the route was
            # derived from evidence and the seed inventory raised nothing. Later events
            # are sticky; direct construction never reaches this.
            self._unknown.discard("bootstrap-provenance-pending")
            self._generation += 1

    def _after_fork(self):
        # A vanished parent thread may own the inherited lock.
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._known = {}
        self.mark_unknown("fork-inherited-attestation")

    def mark_unknown(self, reason):
        with self._lock:
            if reason not in self._unknown:
                self._unknown.add(reason)
                self._generation += 1

    def _seed_functions(self, namespace, module, seen=None):
        seen = set() if seen is None else seen
        for value in tuple(namespace.values()):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if isinstance(value, FunctionType) and value.__module__ == module:
                yield value
                wrapped = getattr(value, "__wrapped__", None)
                if isinstance(wrapped, FunctionType):
                    yield wrapped
            elif isinstance(value, type) and value.__module__ == module:
                yield from self._seed_functions(vars(value), module, seen)

    def _remember(self, code, component):
        for nested in _codes(code):
            if len(self._known) >= MAX_LOADED_CODE:
                self.mark_unknown("loaded-code-limit")
                return
            self._known[id(nested)] = (nested, component)

    def _module_snapshot(self):
        modules = tuple(islice(sys.modules.items(), MAX_UNITS + 1))
        if len(modules) > MAX_UNITS:
            self.mark_unknown("module-inventory-limit")
        return modules[:MAX_UNITS]

    def _inventory_preexisting(self, required=SEED):
        for name, module in self._module_snapshot():
            filename = getattr(module, "__file__", None)
            if not filename:
                continue
            path = Path(filename).resolve()
            if path.is_relative_to(self.core_root):
                if name not in required:
                    self.mark_unknown("prehook-module:" + name)
            elif not self._trusted_stdlib(filename) and not self._dependency_file(
                    path, "prehook-module", name):
                # Only an installer-receipted dependency file (R4-E) is accepted;
                # never reported as audited.
                self.mark_unknown("prehook-unrooted:" + name)
        self._inventory_pth()
        # This is only a comparison of the trusted seed, never retroactive audit.
        for name, relative in required.items():
            module = sys.modules.get(name)
            approved = self._blobs.get(relative)
            path = self.core_root / relative
            if module is None:
                self.mark_unknown("missing-seed:" + name)
                continue
            try:
                source = _read_bounded(path, MAX_SOURCE)
                filename = getattr(module, "__file__", None)
                if (len(source) > MAX_SOURCE or _blob(source) != approved or
                        filename is None or Path(filename).resolve() != path):
                    self.mark_unknown("seed-source-skew:" + name)
                    continue
                expected = tuple(_codes(_compile(source, str(path))))
                for value in self._seed_functions(vars(module), name):
                    if any(_same(value.__code__, code) for code in expected):
                        self._remember(value.__code__, "core")
                    elif not self._interpreter_code(value.__code__):
                        self.mark_unknown("seed-code-skew:" + name)
                frame = sys._getframe()
                while frame:
                    if frame.f_code.co_filename == str(path):
                        if not any(_same(frame.f_code, code) for code in expected):
                            self.mark_unknown("seed-frame-skew:" + name)
                        else:
                            self._remember(frame.f_code, "core")
                    frame = frame.f_back
                self._seed.append({"module": name, "path": relative,
                                   "source_sha256": _sha(source),
                                   "git_blob": hashlib.sha1(
                                       b"blob " + str(len(source)).encode() + b"\0" + source
                                   ).hexdigest(), "kind": "trusted_bootstrap_seed"})
            except (OSError, ValueError, TypeError):
                self.mark_unknown("seed-unreadable:" + name)

    def register_component_root(self, component, path, kind, *, digests=None):
        """Register BEFORE executing a reviewed extension; digests are relative sha256s."""
        root = Path(path).resolve()
        with self._lock:
            if component in self._components or any(
                    root.is_relative_to(r) or r.is_relative_to(root)
                    for r, _, _ in self._components.values()):
                self.mark_unknown("component-root-conflict:" + component)
                return
            if not digests:
                self.mark_unknown("missing-manifest:" + component)
            self._components[component] = (root, kind, dict(digests or {}))
            self._generation += 1
            for name, module in self._module_snapshot():
                filename = getattr(module, "__file__", None)
                if filename and Path(filename).resolve().is_relative_to(root):
                    self.mark_unknown("pre-registration-module:" + name)

    def _owner(self, filename):
        if not filename or filename.startswith("<"):
            return None
        path = Path(filename).resolve()
        for component, (root, kind, digests) in self._components.items():
            if path.is_relative_to(root):
                return component, kind, path, path.relative_to(root).as_posix(), digests
        return None

    def _trusted_stdlib(self, filename):
        if not filename or filename.startswith("<"):
            return False
        path = Path(filename).resolve()
        return (path.is_relative_to(self._stdlib) and
                "site-packages" not in path.parts and "dist-packages" not in path.parts)

    def install_stdlib_generators(self):
        """Only the reviewed CPython 3.14 generators, checked against installed source.

        This extends interpreter trust, not extension coverage. Other Python
        versions and third-party generators remain unsupported.
        """
        if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14):
            self.mark_unknown("unsupported-generator-interpreter")
            return
        self._tls.busy = True
        try:
            import dataclasses
            import collections
            import annotationlib
            targets = (
                ("dataclasses._FuncBuilder.add_fns_to_class",
                 dataclasses._FuncBuilder.add_fns_to_class, dataclasses.__file__, "compile", None),
                ("collections.namedtuple", collections.namedtuple, collections.__file__, "compile", None),
                # R5-2: eval of an annotation string; compile happens in __forward_code__
                # (another frame), so the exec event is verified instead of paired.
                ("annotationlib.ForwardRef.evaluate", annotationlib.ForwardRef.evaluate,
                 annotationlib.__file__, "forwardref", FORWARDREF_TRUST),
            )
            for name, function, filename, kind, trust in targets:
                descriptor = GeneratorDescriptor(name, function, Path(filename),
                                                 dict(self.interpreter_identity),
                                                 kind=kind, trust=trust)
                self._generators[id(descriptor.code)] = descriptor
            self._forwardref = (annotationlib.ForwardRef,
                                annotationlib.ForwardRef.__dict__["__forward_arg__"].fget,
                                annotationlib._rewrite_star_unpack)
        except Exception as exc:  # health: allow BLE001 -- diagnostic boundary latches unknown; must never alter observed execution
            self.mark_unknown("generator-descriptor-error:" + str(exc))
        finally:
            self._tls.busy = False

    def _origin(self, producer):
        """(component, None) for an attested caller, else (None, unattested caller file)."""
        frame = producer.f_back
        while frame is not None:
            known = self._known.get(id(frame.f_code))
            if known is not None and known[0] is frame.f_code:
                return known[1], None
            if not self._interpreter_code(frame.f_code):
                return None, frame.f_code.co_filename
            frame = frame.f_back
        return None, "<top>"

    def _generated_event(self, event, args, frame):
        descriptor = self._generators.get(id(frame.f_code))
        if descriptor is None or descriptor.code is not frame.f_code:
            return False
        # f_lasti can point at a CALL inline-cache word rather than the opcode.
        # Resolve only the current instruction, never a nearby/ancestor call.
        site = descriptor.instruction_sites.get(frame.f_lasti, -1)
        mode = descriptor.sites.get(site)
        if mode is None:
            self.mark_unknown("generated-callsite-mismatch:" + descriptor.name)
            return True
        if descriptor.kind == "forwardref":
            return self._forwardref_event(event, args, frame, descriptor)
        # No trace/profile hook: unmatched frame graphs expire at the next
        # generator event, not immediately on return. Count/byte caps bound
        # retained receipts while there are no further generator events.
        receipts = getattr(self._tls, "receipts", [])
        active = set()
        ancestor = frame
        while ancestor:
            active.add(id(ancestor))
            ancestor = ancestor.f_back
        receipts[:] = [r for r in receipts if id(r[0]) in active]
        self._tls.receipts = receipts
        if event == "compile":
            source, filename = args
            if not isinstance(source, (str, bytes)):
                self.mark_unknown("generated-source-type:" + descriptor.name)
                return True
            if len(source) > MAX_SOURCE:
                self.mark_unknown("generated-receipt-overflow:" + descriptor.name)
                return True
            source = source.encode() if isinstance(source, str) else source
            origin, unattested = self._origin(frame)
            if origin is None:
                # Name the unattested caller: a dependency-origin generation stays
                # unknown, but is distinguishable from a core/plugin escape. Its
                # receipt is still kept (origin None) so the paired exec is consumed
                # as already-unknown instead of being misreported as receipt-missing.
                self.mark_unknown("generated-origin-unknown:" + descriptor.name + ":" + str(unattested))
            if (len(source) > MAX_SOURCE or len(receipts) >= 8 or
                    sum(len(r[2]) for r in receipts) + len(source) > 2 * MAX_SOURCE):
                self.mark_unknown("generated-receipt-overflow:" + descriptor.name)
                return True
            receipts.append((frame, site, source, filename, mode, origin))
            return True
        receipt = next((r for r in reversed(receipts)
                        if r[0] is frame and r[1] == site), None)
        if receipt is None:
            self.mark_unknown("generated-receipt-missing:" + descriptor.name)
            return True
        receipts.remove(receipt)
        _, _, source, filename, mode, origin = receipt
        if origin is None:
            return True  # never recorded as an attested unit; the unknown is latched
        code = args[0]
        if not _same(code, self._compiled(source, filename, mode)):
            self.mark_unknown("generated-code-mismatch:" + descriptor.name)
            return True
        extra = {"producer_distribution": descriptor.distribution} if "dist" in descriptor.distribution else {}
        self._record(code, origin, _sha(source), filename=filename,
                     generator=descriptor.name, producer_sha256=descriptor.source_sha256,
                     producer_site=site, interpreter_sha256=descriptor.distribution.get('binary_sha256'),
                     **extra)
        return True

    def _forwardref_event(self, event, args, frame, descriptor):
        """R5-2: verify the executed annotation expression instead of pairing a compile receipt."""
        if event != "exec" or self._forwardref is None:
            self.mark_unknown("generated-callsite-mismatch:" + descriptor.name)
            return True
        forward_ref, fget, rewrite = self._forwardref
        owner = frame.f_locals.get("self")
        # Exact type: a subclass or duck type could override the arg/code accessors (B1).
        if type(owner) is not forward_ref:
            self.mark_unknown("generated-source-type:" + descriptor.name)
            return True
        try:
            arg = fget(owner)
        except Exception:  # health: allow BLE001 -- uninitialized/odd ForwardRef: latch, never raise from the hook
            arg = None
        if type(arg) is not str or len(arg) > MAX_SOURCE:
            self.mark_unknown("generated-source-type:" + descriptor.name)
            return True
        try:
            expected = self._compiled(rewrite(arg).encode(), "<string>", "eval")
            matches = _same(args[0], expected)
        except (SyntaxError, ValueError, RecursionError, MemoryError, TypeError):
            matches = False
        if not matches:
            self.mark_unknown("generated-code-mismatch:" + descriptor.name)
            return True
        origin, unattested = self._origin(frame)
        if origin is None:
            self.mark_unknown("generated-origin-unknown:" + descriptor.name + ":" + str(unattested))
            return True
        digest = _sha(arg.encode("utf-8", "surrogatepass"))
        key = (origin, descriptor.name, digest)
        if key in self._fwd_seen:
            self._remember(args[0], origin)  # B3/B4: no new unit, but nested code stays attested
            return True
        if len(self._fwd_seen) >= MAX_UNITS:
            self.mark_unknown("executed-unit-limit")
            return True
        self._fwd_seen.add(key)
        self._record(args[0], origin, digest, filename="<string>", generator=descriptor.name,
                     producer_sha256=descriptor.source_sha256, trust=descriptor.trust,
                     interpreter_sha256=descriptor.distribution.get("binary_sha256"))
        return True

    def _module_file(self, name, filename):
        if not filename:
            return
        owner = self._owner(filename)
        if owner is not None or not self._trusted_stdlib(filename):
            if owner is None and self._dependency_module(name, filename):
                return
            if not str(filename).endswith(".py"):
                self.mark_unknown("non-source-module:" + name)
            elif owner is None:
                self.mark_unknown("unrooted-module:" + name)

    def _inventory_modules(self):
        # Requested snapshots only, never idle scans. Recheck mutable __file__ even
        # for an unchanged module object; retaining identity avoids id recycling.
        for name, module in self._module_snapshot():
            filename = getattr(module, "__file__", None)
            self._module_file(name, filename)
            key = (name, id(module))
            identity = (module, filename)
            if self._modules.get(key) == identity:
                continue
            if len(self._modules) >= MAX_UNITS:
                self.mark_unknown("module-inventory-limit")
                continue
            self._modules[key] = identity
            self._generation += 1

    def _audit(self, event, args):
        if event not in ("compile", "exec", "import", "ctypes.dlopen") or getattr(self._tls, "busy", False):
            return
        self._tls.busy = True
        try:
            with self._lock:
                if os.getpid() != self._pid:
                    self.mark_unknown("fork-inherited-attestation")
                if event == "import":
                    # CPython emits the resolved native filename BEFORE loading:
                    # latch now, so deleting sys.modules cannot erase the event.
                    self._generation += 1
                    self._module_file(args[0], args[1])
                    return
                if event == "ctypes.dlopen":
                    self._native_load(args[0], sys._getframe(1))
                    return
                if self._pth_event(event, args, sys._getframe(1)):
                    return
                if self._generated_event(event, args, sys._getframe(1)):
                    return
                if event == "exec":
                    self._observe(args[0], sys._getframe(1))
        except Exception as exc:  # health: allow BLE001 -- diagnostic boundary latches unknown; must never alter observed execution
            # A diagnostic failure must not affect the production execution.
            self.mark_unknown("audit-error:" + type(exc).__name__)
        finally:
            self._tls.busy = False

    # -- R4 dependency-environment custody (installer receipts) -------------------------

    def _dep_unit(self, filename, component, digest, kind, **extra):
        key = (str(filename), kind, extra.get("generator"))
        if key in self._dep_units:
            return
        if len(self._dep_units) >= MAX_DEP_REPORT:
            self.mark_unknown("dependency-report-limit")
            return
        self._generation += 1
        self._dep_units[key] = {"component": component, "kind": kind,
                                "filename": str(filename), "sha256": digest, **extra}

    def register_dependency_environment(self, site_dir):
        """R4-I: core activation names the committed site-packages BEFORE addsitedir.

        Its .pth files are receipt-checked now (before site executes them); a directory
        that is not a site-packages dir latches an unknown instead of widening trust.
        """
        try:
            root = Path(site_dir).resolve()
        except (OSError, ValueError):
            root = None
        self._tls.busy = True
        try:
            with self._lock:
                if root is None or not (self._deps.add_root(root) or root in self._deps.roots):
                    self.mark_unknown("dependency-environment-rejected:" + str(site_dir))
                    return False
                self._generation += 1
                self._inventory_pth()
                return True
        finally:
            self._tls.busy = False

    def _pth_event(self, event, args, frame):
        """R4-I: ``site.addpackage`` executing an ``import`` line of a receipted .pth."""
        code = frame.f_code
        if code.co_name != "addpackage" or not (
                code.co_filename == "<frozen site>" or (
                    self._trusted_stdlib(code.co_filename) and Path(code.co_filename).name == "site.py")):
            return False
        pending = getattr(self._tls, "pth", None)
        if event == "compile":
            self._tls.pth = None
            source = args[0]
            if isinstance(source, bytes):
                try:
                    source = source.decode("utf-8")
                except UnicodeDecodeError:
                    source = None
            fullname = frame.f_locals.get("fullname")
            if not (isinstance(source, str) and isinstance(fullname, str)
                    and self._interpreter_code(code)):
                self.mark_unknown("pth-exec-unverified")
                return True
            path = Path(fullname).resolve()
            receipt = (self._deps.indexed(path) and self._dep_ready()
                       and self._deps.verify(path)) or None
            try:
                lines = _read_bounded(path, MAX_SOURCE).decode("utf-8").splitlines()
            except (OSError, ValueError):
                lines = ()
            if receipt is None or source not in lines:
                self.mark_unknown("pth-exec-unreceipted:" + fullname)
                return True
            self._tls.pth = (frame, source, path, receipt)
            return True
        self._tls.pth = None
        if pending is None or pending[0] is not frame:
            self.mark_unknown("pth-exec-unverified")
            return True
        _, source, path, receipt = pending
        if not _same(args[0], self._compiled(source.encode(), "<string>")):
            self.mark_unknown("pth-exec-mismatch:" + str(path))
            return True
        component = "dependency:" + receipt[0]
        self._dep_unit(path, component, receipt[1], "pth-exec", line=source)
        self._remember(args[0], component)
        return True

    def _dep_ready(self):
        self._deps.build()
        if self._deps.limit:
            self.mark_unknown("dependency-index-limit")
            return False
        return True

    def _dependency_file(self, path, kind, name):
        """Receipt a pre-hook/native dependency file by its current bytes (R4-E, step 4)."""
        if not self._deps.indexed(path) or not self._dep_ready():
            return False
        receipt = self._deps.verify(path)
        if receipt is None:
            self.mark_unknown("dependency-unreceipted:" + str(path))
            return False
        self._dep_unit(path, "dependency:" + receipt[0], receipt[1], kind, module=name)
        return True

    def _dependency_module(self, name, filename):
        try:
            path = Path(filename).resolve()
        except (OSError, ValueError):
            return False
        if not self._deps.indexed(path) or not self._dep_ready():
            return False
        if path.suffix == ".py":
            # Source bytes are verified when the code executes (_dependency_exec);
            # an import needs only a receipt row.
            if path in self._deps.rows:
                return True
            self.mark_unknown("dependency-unreceipted:" + str(path))
            return False
        if (str(path), "native", None) in self._dep_units:
            return True
        return self._dependency_file(path, "native", name)

    def _inventory_pth(self):
        """R4-E: every .pth and site/user customize hook in an indexed dir is receipted."""
        for root in self._deps.roots:
            for pth in sorted(root.glob("*.pth"))[:MAX_DEP_REPORT]:
                if not self._dependency_file(pth.resolve(), "pth", pth.name):
                    self.mark_unknown("pth-unreceipted:" + pth.name)
            for hook in ("sitecustomize", "usercustomize"):
                path = root / (hook + ".py")
                if path.exists() and not self._dependency_file(path.resolve(), "prehook-module", hook):
                    self.mark_unknown("prehook-unrooted:" + hook)

    def _dependency_exec(self, code, producer=None):
        filename = code.co_filename
        try:
            path = Path(filename).resolve()
        except (OSError, ValueError):
            path = None
        if path is None or filename.startswith("<") or not self._deps.indexed(path):
            self.mark_unknown("unrooted-exec:" + filename)
            if producer is not None and len(self._unknown_detail) < MAX_DEP_REPORT:
                # Diagnosis only (which code produced it); the unknown stays latched.
                self._unknown_detail.setdefault("unrooted-exec:" + filename, set()).add(
                    f"{producer.f_code.co_filename}:{producer.f_code.co_qualname}")
            return
        if not self._dep_ready():
            return
        try:
            source = _read_bounded(path, MAX_SOURCE)
        except (OSError, ValueError):
            self.mark_unknown("dependency-unreadable:" + filename)
            return
        receipt = self._deps.verify(path, source)
        if receipt is None:
            self.mark_unknown(("dependency-skew:" if path in self._deps.rows
                               else "dependency-unreceipted:") + filename)
            return
        if not _same(code, self._compiled(source, filename)):
            self.mark_unknown("executed-code-mismatch:" + filename)
            return
        component = "dependency:" + receipt[0]
        self._dep_unit(path, component, receipt[1], "source")
        self._remember(code, component)
        self._dependency_generators(path, code, source)

    def _dependency_generators(self, path, code, source):
        """R5-1: descriptors for the finite producer table, from the receipted bytes (A1/A3)."""
        identity = self._deps.identity(path)  # from the RECORD row, not co_filename text (A3)
        if identity is None or code.co_qualname != "<module>":
            return
        dist, version, relative = identity
        key = (dist, relative)
        wanted = _DEPENDENCY_GENERATORS.get(key)
        if wanted is None:
            return
        for stale in self._dep_generators.pop(key, ()):  # A4: reload replaces, never stacks
            self._generators.pop(stale, None)
        info = self._deps.dists.get(self._deps.rows[path][0], {})
        distribution = {"dist": dist, "version": version,
                        "record_sha256": info.get("record_sha256")}
        created = []
        for qualname in wanted:
            name = dist + ":" + qualname
            try:
                descriptor = GeneratorDescriptor.from_executed(name, code, qualname, source, distribution)
            except ValueError as exc:  # A2: never a generic audit-error
                self.mark_unknown(str(exc) if str(exc).startswith("generator-callsite-missing:")
                                  else "generator-descriptor-error:" + str(exc))
                continue
            except (SyntaxError, RecursionError, MemoryError) as exc:
                self.mark_unknown("generator-descriptor-error:" + type(exc).__name__)
                continue
            self._generators[id(descriptor.code)] = descriptor
            created.append(id(descriptor.code))
        self._dep_generators[key] = created

    def _native_caller(self, frame):
        ctypes_dir = self._stdlib / "ctypes"
        while frame is not None and Path(frame.f_code.co_filename).is_relative_to(ctypes_dir):
            frame = frame.f_back
        if frame is None:
            return None
        known = self._known.get(id(frame.f_code))
        if known is not None and known[0] is frame.f_code:
            kind = self._components.get(known[1], (None, None, None))[1]
            return known[1] if kind == "core" or known[1].startswith("dependency:") else None
        return "stdlib" if self._interpreter_code(frame.f_code) else None

    def _native_load(self, name, frame):
        """R4-D: finite sonames or a receipted absolute path, from a non-plugin caller."""
        caller = self._native_caller(frame)
        allowed = name in _NATIVE_SONAMES
        if not allowed and isinstance(name, str) and os.path.isabs(name):
            path = Path(name).resolve()
            allowed = (self._deps.indexed(path) and self._dep_ready()
                       and self._deps.verify(path) is not None)
        if caller is None or not allowed:
            self.mark_unknown("native-load:" + str(name))
            return
        if len(self._native_loads) < MAX_DEP_REPORT:
            self._native_loads.append({"name": name, "caller_component": caller})

    def dependency_receipts(self):
        """Receipts actually used: trusted by installer receipt, not audited, no coverage."""
        with self._lock:
            return {"trust": DEPENDENCY_TRUST, "roots": [str(r) for r in self._deps.roots],
                    "dists": {name: {k: v for k, v in info.items() if k != "used"}
                              for name, info in self._deps.dists.items() if info["used"]},
                    "units": [dict(u) for u in self._dep_units.values()],
                    "native_loads": [dict(n) for n in self._native_loads],
                    "unknown_producers": {k: sorted(v) for k, v in self._unknown_detail.items()},
                    "generated_trust": {"annotationlib.ForwardRef.evaluate": FORWARDREF_TRUST},
                    "residual_risks": [
                        "RECORD itself is not receipted",
                        "native bytes are hashed before load (TOCTOU); loader-resolved "
                        "transitive libraries are not audited"]}

    def _compiled(self, source, filename, mode="exec"):
        # Every caller supplies freshly bounded bytes; no stat/mtime trust. Only
        # compilation is cached. Filename/mode/optimization remain in the key.
        if len(source) > MAX_SOURCE:
            raise ValueError("source-limit")
        key = (_sha(source), filename, mode, sys.flags.optimize)
        with self._lock:
            cached = self._source_cache.get(key)
            if cached is not None:
                self._source_cache.move_to_end(key)
                return cached[0]
            code = _compile(source, filename, mode)
            size = _retained_size(code) + _retained_size(key)
            if size <= MAX_CACHE_BYTES:
                while self._source_cache and (
                        len(self._source_cache) >= MAX_CACHE_ENTRIES or
                        self._cache_bytes + size > MAX_CACHE_BYTES):
                    _, (_, removed) = self._source_cache.popitem(last=False)
                    self._cache_bytes -= removed
                self._source_cache[key] = (code, size)
                self._cache_bytes += size
            return code

    def _interpreter_code(self, code, *, module=False):
        filename = code.co_filename
        if filename.startswith("<frozen ") and filename.endswith(">"):
            name = filename[len("<frozen "):-1]
            # The import system is frozen under its bootstrap aliases, not its
            # display names; this finite map is the only translation accepted.
            name = _FROZEN_ALIASES.get(name, name)
            if not _imp.is_frozen(name):
                return False
            expected = _imp.get_frozen_object(name)
        elif self._trusted_stdlib(filename):
            source = _read_bounded(Path(filename), MAX_SOURCE)
            if len(source) > MAX_SOURCE:
                return False
            expected = self._compiled(source, filename)
        else:
            return False
        return _same(code, expected) if module else any(
            _same(code, candidate) for candidate in _codes(expected))

    def _observe(self, code, producer=None):
        owner = self._owner(code.co_filename)
        if owner is None:
            filename = code.co_filename
            if self._trusted_stdlib(filename):
                if not self._interpreter_code(code, module=True):
                    self.mark_unknown("stdlib-code-mismatch:" + filename)
            elif filename.startswith("<frozen "):
                if not self._interpreter_code(code, module=True):
                    self.mark_unknown("frozen-code-mismatch:" + filename)
            else:
                self._dependency_exec(code, producer)
            return
        component, kind, path, relative, digests = owner
        source = _read_bounded(path, MAX_SOURCE)
        if len(source) > MAX_SOURCE:
            self.mark_unknown("source-limit:" + str(path))
            return
        digest = _sha(source)
        approved = self._blobs.get(relative) if kind == "core" else None
        if ((kind == "core" and _blob(source) != approved) or
                (kind != "core" and digests.get(relative) != digest)):
            self.mark_unknown("source-skew:" + str(path))
            return
        if not _same(code, self._compiled(source, code.co_filename)):
            self.mark_unknown("executed-code-mismatch:" + str(path))
            return
        self._record(code, component, digest, filename=code.co_filename)

    def _record(self, code, component, digest, **extra):
        if component.startswith("dependency:"):
            # Dependency-origin generated code: a receipt, never an audited unit (R4-H).
            detail = {k: extra[k] for k in ("producer_sha256", "producer_distribution", "trust")
                      if extra.get(k) is not None}
            self._dep_unit(extra.get("filename", code.co_filename), component, digest,
                           "generated", generator=extra.get("generator"), **detail)
            self._remember(code, component)
            return
        if len(self._units) >= MAX_UNITS:
            self.mark_unknown("executed-unit-limit")
            return
        self._generation += 1
        self._units.append({"component": component, "source_sha256": digest,
                            "generation": self._generation, **extra})
        self._remember(code, component)

    def attest_code(self, code):
        """Identity-bind a currently loaded callback, never trust filename alone."""
        with self._lock:
            known = self._known.get(id(code))
            if known is not None and known[0] is code:
                return True
            self.mark_unknown("unobserved-code:" + code.co_filename)
            return False

    def executed_units(self):
        with self._lock:
            return tuple(dict(unit) for unit in self._units)

    def trusted_bootstrap_seed(self):
        return tuple(dict(unit) for unit in self._seed)

    def route_report(self):
        """Route and launcher identity beside the seed; recorded once, sticky."""
        return {"route": self.route, "stub_head_digest": self.stub_head_digest,
                "launcher": dict(self._launcher) if self._launcher else None,
                "seed_required": sorted(SEED_COMMON if self.route else SEED),
                "main_is_seed": False if self.route == ROUTE_MANAGED_STUB else None,
                "interpreter": dict(self.interpreter_identity)}

    def unknowns(self):
        with self._lock:
            self._inventory_modules()
            return tuple(sorted(self._unknown))

    def generation(self):
        with self._lock:
            return self._generation


_installed = None


def install(core_root, boot_sha):
    """Install once at the reviewed bootstrap boundary; route is derived HERE.

    The route comes only from ``sys.orig_argv``/``sys.argv``, the ``__main__`` frame
    chain and the boot-tree blobs; no parameter, environment value or ``sys`` flag
    participates. Provenance-pending is cleared only by this function's own path when
    the supported route was verified and the seed inventory is clean. Any other launch
    (console, ``-m``, relaunch, ``--run-module``, runtime_command ``-c`` ...) latches a
    sticky ``unsupported-route:*`` unknown and installs no hook.
    """
    global _installed
    if _installed is None:
        _installed = Attestation(core_root, boot_sha, _install_key=_INSTALL_KEY)
    elif (_installed.core_root != Path(core_root).resolve() or
          _installed.boot_sha != boot_sha):
        _installed.mark_unknown("installation-identity-change")
    return _installed


def route_report():
    return _ledger().route_report()


def trusted_bootstrap_seed():
    return _ledger().trusted_bootstrap_seed()


def _ledger():
    if _installed is None:
        raise RuntimeError("code attestation not installed")
    return _installed


def register_component_root(component, path, kind, *, digests=None):
    return _ledger().register_component_root(component, path, kind, digests=digests)


def executed_units():
    return _ledger().executed_units()


def dependency_receipts():
    return _ledger().dependency_receipts()


def register_dependency_environment(site_dir):
    """Called by core dependency activation before ``site.addsitedir``; no-op if uninstalled."""
    if _installed is None:
        return False
    return _installed.register_dependency_environment(site_dir)


def unknowns():
    return _ledger().unknowns()


def generation():
    return _ledger().generation()
