"""Observational executed-code receipts, not a sandbox or a coverage approval.

Install at the reviewed bootstrap boundary. Direct construction supports isolated
fixtures but never establishes bootstrap provenance. Source manifests are review
inputs, not permission to declare arbitrary code passive. No third-party imports.
"""
import ast
import atexit
import base64
import csv
import dis
import _imp
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import sysconfig
from collections import Counter, OrderedDict, deque
from itertools import islice
import threading
import time
from types import CodeType, FunctionType
from typing import TYPE_CHECKING

# Load the stdlib-only observer before an audit ledger can be activated. A lazy
# import inside its first inventory would itself change the observed code graph.
from hermes_cli.code_inventory_diagnostics import InventoryObservation, inventory_modules

if TYPE_CHECKING:
    from hermes_cli.code_attestation_snapshot import DiagnosticSnapshot


# Required in every supported route (missing => unknown). Blob-compared, never audited.
SEED_COMMON = {
    "hermes_bootstrap": "hermes_bootstrap.py",
    "hermes_cli": "hermes_cli/__init__.py",
    "hermes_cli._early_recovery": "hermes_cli/_early_recovery.py",
    "hermes_cli.git_fingerprint": "hermes_cli/git_fingerprint.py",
    "hermes_cli.code_attestation": "hermes_cli/code_attestation.py",
    "hermes_cli.code_inventory_diagnostics": "hermes_cli/code_inventory_diagnostics.py",
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
# A ``venv_sync.relaunch_command`` child of the managed stub (the MacBook gateway).
ROUTE_MANAGED_STUB_RELAUNCH = "managed-stub-relaunch"
ROUTE_MANAGED_WORKER_STUB = "managed-worker-stub"
STUB_ROUTES = (ROUTE_MANAGED_STUB, ROUTE_MANAGED_STUB_RELAUNCH, ROUTE_MANAGED_WORKER_STUB)
# Seed for the relaunch route ONLY: the stdlib modules its prefix imports before the hook.
RELAUNCH_PREFIX_MODULES = ("runpy", "importlib.util", "pathlib")
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
# Module executions whose source comparison is still owed. Every ledger read settles
# them first, so a pending comparison is never reported as verified.
MAX_PENDING = 8192
SETTLE_DELAY = 6.0  # seconds after the first deferral before the background settler works
SETTLE_DUTY = 0.25  # share of one core the settler may take from the importing thread
_FROZEN_ALIASES: dict[str, str] = {"importlib._bootstrap": "_frozen_importlib",
                   "importlib._bootstrap_external": "_frozen_importlib_external"}


MAX_DISTS = 4096
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_DEP_REPORT = 4096
# Measured Mac managed environment has 23,655 receipted Python files; its live
# ledger filled 4096 (4009 source, 87 other). Bound unit retention separately
# from ancillary reports so ordinary SDK imports do not exhaust a distro-sized cap.
MAX_DEP_UNITS = 32768
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
# Reviewed producer, not an attrs filename/package exemption. Output also needs
# observed compile/exec custody and the exact internal generating caller below.
_ATTRS_IDENTITY = ("attrs", "26.1.0", "attr/_make.py")
_ATTRS_SOURCE = "1fb387d9e592e429c1ccb5233451355b299f3eb985d6bf1fbf644f76df4cb980"
_ATTRS_DIRECT = frozenset({"_add_hash", "_add_eq", "_add_repr"})
_ATTRS_CACHED = frozenset({"_ClassBuilder._eval_snippets", "_make_cached_property_getattr"})


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
        self._owners = {}
        self._ambiguous = set()
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
            path = root / relative
            if path in self.rows:
                self._ambiguous.add(path)
            self.rows[path] = (dist, digest, size)
            self._owners[path] = (root, dist)

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
                self.dists[(root, info.name[:-len(".dist-info")])] = {
                    "dir": str(root), "record_sha256": _sha(record),
                    "installer_sha256": installer, "used": False}
                self._parse(root, info, record.decode("utf-8", "replace"))

    def indexed(self, path):
        return any(path.is_relative_to(root) for root in self.roots)

    def identity(self, path):
        """(canonical PEP 503 dist name, version, relative posix path) of a RECORD row, else None."""
        row = self.rows.get(path)
        if row is None or path in self._ambiguous:
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
        if row is None or path in self._ambiguous:
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
        self.distribution(path)["used"] = True
        return dist, digest

    def distribution(self, path):
        """The exact installer/root that owns this unique source claim."""
        return self.dists[self._owners[path]]

    def receipts(self):
        # Preserve single-generation names, qualify overlaps instead of overwriting.
        counts = Counter(name for _, name in self.dists)
        return {(name if counts[name] == 1 else str(root / name)):
                {k: v for k, v in info.items() if k != "used"}
                for (root, name), info in self.dists.items() if info["used"]}


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
        if (kind == b"blob" and mode in (b"100644", b"100755")
                and (filename.endswith(b".py")
                     or filename == b"hermes_cli/data/plugin-review-records.json")):
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
    Returns a dict for a supported managed-stub route; raises ``_Refuse`` otherwise.
    """
    orig = list(getattr(sys, "orig_argv", ()))
    argv = list(sys.argv)
    if _relaunch_shaped(orig, template):
        # A ``venv_sync.relaunch_command`` child: its own route, proven byte-exact below.
        # The routing test reads argv bytes only; everything it admits is re-proven in full.
        return _detect_relaunch_route(core_root, template, own_file, orig, argv)
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
    head, tail, head_digest, current_digest = _check_stub_script(core_root, template, script, argv, allow_worker=True)
    if "hermes_cli.main" in sys.modules:
        raise _Refuse("unexpected-prehook:hermes_cli.main")
    frame = _verify_frame_chain(core_root, own_file, script, head)
    worker = script == template.render(str(core_root), "agent.managed_worker", "main")
    return {"route": ROUTE_MANAGED_WORKER_STUB if worker else ROUTE_MANAGED_STUB,
            "stub_head_digest": head_digest,
            "head_sha256": head_digest, "tail_sha256": _sha(tail.encode()),
            "script_sha256": _sha(script.encode()), "launcher_argv0": orig[0],
            "current_template_head_digest": current_digest, "code": frame.f_code}


def _check_stub_script(core_root, template, script, argv, *, allow_worker=False):
    """The managed stub script's identity, shared unchanged by both stub routes: argv
    refusals, pin marker, byte-exact tail, root literal, reviewed head digest."""
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
        # Only this entire current fixed worker script is admitted, not an
        # arbitrary module tail or a worker variant of a historical launcher.
        if not allow_worker or script != template.render(str(core_root), "agent.managed_worker", "main"):
            raise _Refuse("unsupported-route:stub-tail")
    root = str(core_root)
    if repr(root) not in head:
        raise _Refuse("unsupported-stub-root")
    head_digest = _sha(template.normalized_head(head, root).encode())
    if head_digest not in STUB_HEAD_DIGESTS | {current_digest}:
        raise _Refuse("unsupported-stub-head")
    return head, tail, head_digest, current_digest


def _relaunch_shaped(orig, template):
    """Routing only: the source after the first ``-c`` starts with the relaunch head."""
    if "-c" not in orig[1:]:
        return False
    index = orig.index("-c", 1)
    source = orig[index + 1] if index + 1 < len(orig) else None
    return isinstance(source, str) and source.startswith(template.RELAUNCH_HEAD)


def _detect_relaunch_route(core_root, template, own_file, orig, argv):
    """Route ``managed-stub-relaunch``: ``orig_argv == [exe, "-I" (one or more, nothing else),
    "-c", src, *sys.argv[1:]]`` with ``src == relaunch_prefix(core_root, sys.argv) + "exec(" + repr(script) + ")"``
    byte for byte, and ``script`` passing the unchanged managed-stub checks. Flags and env are
    never evidence; ``_hermes_relaunched`` is set by these very bytes."""
    index = orig.index("-c", 1)
    options = orig[1:index]
    if not options or any(option != "-I" for option in options):
        raise _Refuse("unsupported-route:relaunch-options")
    # Anything after the source is exactly sys.argv's tail (venv_sync keeps a ``-c`` program's trailing argv for
    # its command line); the prefix proven below assigns that same sys.argv, so the tokens are never evidence.
    if orig[index + 2:] != argv[1:] or argv[:1] != ["-c"]:
        raise _Refuse("unsupported-route:relaunch-argv")
    source = orig[index + 1]
    if len(source) > MAX_SOURCE:
        raise _Refuse("unsupported-route:relaunch-source")
    # Byte-exact: the prefix for (core_root, sys.argv), then exactly the canonical exec body.
    prefix = template.relaunch_prefix(str(core_root), argv)
    if not source.startswith(prefix):
        raise _Refuse("unsupported-route:relaunch-source")
    script = _relaunch_embedded_script(source[len(prefix):])
    if source != prefix + "exec(" + repr(script) + ")":
        raise _Refuse("unsupported-route:relaunch-source")
    _, tail, head_digest, current_digest = _check_stub_script(core_root, template, script, argv)
    if "hermes_cli.main" in sys.modules:
        raise _Refuse("unexpected-prehook:hermes_cli.main")
    prefix_modules = _relaunch_prefix_modules()
    frame = _verify_relaunch_frames(core_root, own_file, source)
    return {"route": ROUTE_MANAGED_STUB_RELAUNCH, "stub_head_digest": head_digest,
            "head_sha256": head_digest, "tail_sha256": _sha(tail.encode()),
            "script_sha256": _sha(script.encode()), "launcher_argv0": orig[0],
            "current_template_head_digest": current_digest, "code": frame.f_code,
            "relaunch_source_sha256": _sha(source.encode()), "relaunch_options": len(options),
            "prefix_modules": prefix_modules, "script_code": _compile(script, "<string>"),
            # Data, not evidence (traced): venv_sync.exec_launch sets it for the exec'd child
            # and prepare_launch pops it after this install, so a POSIX relaunch has it here.
            "launch_handoff_env": LAUNCH_HANDOFF_ENV in os.environ}


def _relaunch_embedded_script(body):
    """The literal inside ``exec(...)``, recovered by ``ast.literal_eval`` (never evaluated)."""
    if not (body.startswith("exec(") and body.endswith(")")):
        raise _Refuse("unsupported-route:relaunch-body")
    try:
        script = ast.literal_eval(body[len("exec("):-1])
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise _Refuse("unsupported-route:relaunch-body") from None
    if not isinstance(script, str):
        raise _Refuse("unsupported-route:relaunch-body")
    return script


def _relaunch_prefix_modules():
    """Finite seed for this route only: the stdlib modules the relaunch prefix imports before
    the hook must be installed-stdlib source files; each is reported with its content digest."""
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    found = []
    for name in RELAUNCH_PREFIX_MODULES:
        filename = getattr(sys.modules.get(name), "__file__", None)
        path = Path(filename).resolve() if filename else None
        if (path is None or not path.is_relative_to(stdlib) or path.suffix != ".py"
                or "site-packages" in path.parts or "dist-packages" in path.parts):
            raise _Refuse("unsupported-route:relaunch-prefix-module:" + name)
        try:
            digest = _sha(_read_bounded(path, MAX_SOURCE))
        except (OSError, ValueError):
            raise _Refuse("unsupported-route:relaunch-prefix-module:" + name) from None
        found.append({"module": name, "path": str(path), "sha256": digest,
                      "kind": "relaunch_prefix_stdlib"})
    return found


def _outer_launch_frame(core_root, own_file):
    """install <- (one bootstrap helper) <- hermes_bootstrap module frame <- importlib frames
    only <- the outermost ``__main__`` ``<string>`` frame, which is returned."""
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
    return frame


def _verify_frame_chain(core_root, own_file, script, head):
    """Return the outermost ``<string>`` frame proven to be ``script``; raise ``_Refuse`` otherwise."""
    frame = _outer_launch_frame(core_root, own_file)
    if not _same(frame.f_code, _compile(script, "<string>")):
        raise _Refuse("unsupported-route:frame-code")
    marker_line = head.count("\n") + 1  # the pin marker line; try: and import follow
    lines = script.splitlines()
    if (frame.f_lineno != marker_line + 2 or lines[frame.f_lineno - 1].strip() != "import hermes_bootstrap"
            or lines[frame.f_lineno - 2].strip() != "try:"):
        raise _Refuse("unsupported-route:frame-line")
    return frame


def _verify_relaunch_frames(core_root, own_file, source):
    """Relaunch: the prefix ran the bootstrap through ``spec.loader.exec_module``, so the outermost
    frame must be exactly ``compile(source)`` at its single line."""
    frame = _outer_launch_frame(core_root, own_file)
    if not _same(frame.f_code, _compile(source, "<string>")):
        raise _Refuse("unsupported-route:frame-code")
    if "\n" in source or "\r" in source or frame.f_lineno != 1:
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
        self._launcher_exec = None  # relaunch: (prefix frame code, embedded script code), one-shot
        self._refused = False
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._unknown = {"bootstrap-provenance-pending"}
        self._extension_unknowns = set()
        self._blocking_unknowns = set()
        self._generation = 0
        self._units = []
        self._known = {}  # identity-keyed strong references: no id recycling
        self._pending = deque()  # FIFO of [kind, payload, settled, result, verdicts] (see _enqueue)
        self._pending_index = {}  # id(nested code) -> its pending entry (entry holds the reference)
        self._settler = None
        self._stopping = self._exit_hooked = False
        self._deferring = False  # set only by defer_comparisons(), never cleared
        self._demand = threading.Event()  # a bounded reader ran out of budget: settle at full speed
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
        self._unknown_sources = {}  # diagnostic producer receipts, never generated-code trust
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
            if route["route"] == ROUTE_MANAGED_STUB_RELAUNCH:
                self._launcher.update(
                    relaunch_source_sha256=route["relaunch_source_sha256"],
                    relaunch_options=route["relaunch_options"],
                    launch_handoff_env=route["launch_handoff_env"])
                # The prefix later exec()s the embedded script: exactly that code object, from
                # exactly the proven prefix frame, is the trusted launcher (_launcher_event).
                self._launcher_exec = (route["code"], route["script_code"])
                self._seed.extend(route["prefix_modules"])
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
        self._pending = deque()
        self._pending_index = {}
        self._settler = None
        self._stopping = self._exit_hooked = False
        self._demand = threading.Event()
        self.mark_unknown("fork-inherited-attestation")

    def mark_unknown(self, reason):
        with self._lock:
            self.__dict__.setdefault("_blocking_unknowns", set()).add(reason)
            if reason not in self._unknown:
                self._unknown.add(reason)
                self._generation += 1

    def _extension_unknown(self, reason, filename):
        """Latch provenance at emission, never infer a module's path from its later name.

        Only these four emitters may downgrade an unknown. Core-root paths, including
        symlink aliases, and unreadable provenance stay blocking. Raw ledger evidence
        remains unchanged; this partition is only the quiet CPU measurement policy.
        """
        if not reason.startswith(("unrooted-exec:", "unrooted-module:",
                                  "generated-origin-unknown:", "native-load:")):
            self.mark_unknown(reason)
            return
        try:
            core = (not filename or str(filename).startswith("<")
                    or Path(filename).resolve().is_relative_to(self.core_root))
        except (OSError, ValueError, TypeError):
            core = True
        with self._lock:
            if core:
                self.mark_unknown(reason)
            else:
                self._extension_unknowns.add(reason)
                if reason not in self._unknown:
                    self._unknown.add(reason)
                    self._generation += 1

    def quiet_unknown_partition(self, *, inventory=False):
        """Blocking and informational unknowns, from one locked ledger observation."""
        with InventoryObservation(self, enabled=inventory) as diagnostics:
            if inventory:
                self._settle()
                self._inventory_modules(diagnostics=diagnostics)
            else:
                self.latched_unknowns()
            extension = self._extension_unknowns - self._blocking_unknowns
            return sorted(self._unknown - extension), sorted(extension)

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
            self._settle_under(root)
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
        return self._owner_path(Path(filename).resolve())

    def _owner_path(self, path):
        # POSIX containment is an exact parts-prefix comparison. pathlib's
        # is_relative_to builds/searches every ancestor for each component root;
        # a module outside all plugins pays that cost for every installed plugin.
        # Keep Windows' case-folding/drive semantics on its existing pathlib path.
        parts = path.parts if isinstance(path, PurePosixPath) else None
        for component, (root, kind, digests) in self._components.items():
            if parts is not None and isinstance(root, PurePosixPath):
                if path.anchor != root.anchor or parts[:len(root.parts)] != root.parts:
                    continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            return component, kind, path, relative.as_posix(), digests
        return None

    def _trusted_stdlib(self, filename):
        if not filename or filename.startswith("<"):
            return False
        return self._trusted_stdlib_path(Path(filename).resolve())

    def _trusted_stdlib_path(self, path):
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
        return self._origin_codes(self._caller_codes(producer))

    @staticmethod
    def _caller_codes(producer):
        codes = []
        frame = producer.f_back
        while frame is not None:
            codes.append(frame.f_code)
            frame = frame.f_back
        return tuple(codes)

    def _origin_codes(self, codes):
        for code in codes:
            self._settle_code(code)
            known = self._known.get(id(code))
            if known is not None and known[0] is code:
                return known[1], None
            if not self._interpreter_code(code):
                return None, code.co_filename
        return None, "<top>"

    def _deferred_origin(self, descriptor, codes):
        """Queue a generator's origin, fixed to the modules executed so far.

        Each caller is bound now to the one execution that can vouch for it: the
        component it already compared clean under, or the pending execution that
        owns it, whose own outcome is read once it settles. A later execution of
        the same code object can never vouch for this call.
        """
        index, known = self._pending_index, self._known
        marks = []
        for code in codes:
            entry = index.get(id(code))
            if entry is not None:
                # The newest execution decides (as the inline ledger's last write does).
                if entry[4] is None:
                    entry[4] = {}
                entry[4].setdefault(id(code), [code, None])
                marks.append((code, entry))
                continue
            attested = known.get(id(code))
            if attested is not None and attested[0] is code:
                marks.append((code, attested[1]))
                break  # decided at the call; nothing below it is consulted
            marks.append((code, None))
        return self._enqueue(["origin", (descriptor, tuple(marks)), False, None, None])

    def _settled_origin(self, descriptor, marks):
        for code, mark in marks:
            if isinstance(mark, list):
                self._settle_entry(mark)
                mark = mark[4][id(code)][1]  # that execution's verdict, not a later one's
            if mark is not None:
                return mark
            if not self._interpreter_code(code):
                return self._generated_origin(descriptor, None, code.co_filename)
        return self._generated_origin(descriptor, None, "<top>")

    def _generated_origin(self, descriptor, codes, unattested=None):
        origin = None
        if codes is not None:
            origin, unattested = self._origin_codes(codes)
        if origin is None:
            # Name the unattested caller: a dependency-origin generation stays
            # unknown, but is distinguishable from a core/plugin escape. Its
            # receipt is still kept (origin None) so the paired exec is consumed
            # as already-unknown instead of being misreported as receipt-missing.
            self._extension_unknown("generated-origin-unknown:" + descriptor.name + ":" + str(unattested), unattested)
        return origin

    def _generated_unit(self, descriptor, site, code, source, filename, mode, origin):
        if origin is None:
            return  # never recorded as an attested unit; the unknown is latched
        if not _same(code, self._compiled(source, filename, mode)):
            self.mark_unknown("generated-code-mismatch:" + descriptor.name)
            return
        extra = {"producer_distribution": descriptor.distribution} if "dist" in descriptor.distribution else {}
        self._record(code, origin, _sha(source), filename=filename,
                     generator=descriptor.name, producer_sha256=descriptor.source_sha256,
                     producer_site=site, interpreter_sha256=descriptor.distribution.get('binary_sha256'),
                     **extra)

    def _generated_event(self, event, args, frame):
        descriptor = self._generators.get(id(frame.f_code))
        if descriptor is None or descriptor.code is not frame.f_code:
            return False
        if descriptor.kind == "attrs":
            return self._attrs_event(event, args, frame, descriptor)
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
            codes = self._caller_codes(frame)
            if self._deferring and os.getpid() == self._pid:
                origin = self._deferred_origin(descriptor, codes)
            else:
                origin = self._generated_origin(descriptor, codes)
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
        code = args[0]
        if isinstance(origin, list):
            if isinstance(code, CodeType):
                self._own(code, self._enqueue(
                    ["generated", (descriptor, site, code, source, filename, mode, origin), False, None, None]))
                return True
            self._settle_entry(origin)
            origin = origin[3]
        self._generated_unit(descriptor, site, code, source, filename, mode, origin)
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
            self._extension_unknown("generated-origin-unknown:" + descriptor.name + ":" + str(unattested), unattested)
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

    def _launcher_event(self, code, frame):
        """Relaunch route: the one ``exec`` the proven prefix frame makes is the embedded stub
        script, attributed to the trusted launcher only when it is that exact code object."""
        pending = self._launcher_exec
        if pending is None or frame is None or frame.f_code is not pending[0]:
            return False
        self._launcher_exec = None  # one-shot: any later exec from that frame is observed as usual
        if not _same(code, pending[1]):
            self.mark_unknown("relaunch-exec-mismatch")
            return True
        self._generation += 1
        self._launcher["script_exec_observed"] = True  # route_report's launcher, not the seed copy
        return True

    def _module_file(self, name, filename, *, diagnostics=None):
        if not filename:
            return
        # Resolve freshly even for unchanged identities; timing never caches paths.
        path = None if filename.startswith("<") else (
            diagnostics.call('resolve', Path(filename).resolve) if diagnostics is not None
            else Path(filename).resolve())
        owner = (diagnostics.call('owner', self._owner_path, path) if diagnostics is not None
                 else self._owner_path(path)) if path is not None else None
        if diagnostics is not None:
            return diagnostics.call('classify', self._classify_module_file, name, filename, path, owner)
        return self._classify_module_file(name, filename, path, owner)

    def _classify_module_file(self, name, filename, path, owner):
        if owner is not None or path is None or not self._trusted_stdlib_path(path):
            if owner is None and path is not None and self._dependency_module_path(name, path):
                return
            if not str(filename).endswith(".py"):
                self.mark_unknown("non-source-module:" + name)
            elif owner is None:
                self._extension_unknown("unrooted-module:" + name, filename)

    def _inventory_modules_diagnostic(self, *, deadline=None, progress=None):
        from hermes_cli.code_attestation_inventory import inventory_modules
        return inventory_modules(self, deadline=deadline, progress=progress)

    def _inventory_modules(self, *, deadline=None, diagnostics=None):
        return inventory_modules(self, deadline=deadline, diagnostics=diagnostics)

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
                # A pending producer module registers its generator descriptors
                # only when settled: do that before classifying its output.
                self._settle_code(sys._getframe(1).f_code)
                if self._pth_event(event, args, sys._getframe(1)):
                    return
                if self._generated_event(event, args, sys._getframe(1)):
                    return
                if event == "exec" and self._launcher_event(args[0], sys._getframe(1)):
                    return
                if event == "exec":
                    (self._defer if self._deferring else self._observe)(args[0], sys._getframe(1))
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
        if len(self._dep_units) >= MAX_DEP_UNITS:
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
        with self._lock:
            self._settle()  # executions so far are judged against the roots they ran under
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
        return self._dependency_module_path(name, path)

    def _dependency_module_path(self, name, path):
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
        producer = getattr(producer, "f_code", producer)
        filename = code.co_filename
        try:
            path = Path(filename).resolve()
        except (OSError, ValueError):
            path = None
        if path is None or filename.startswith("<") or not self._deps.indexed(path):
            provenance = (producer.co_filename if filename.startswith("<") and producer is not None
                          else filename)
            self._extension_unknown("unrooted-exec:" + filename, provenance)
            if producer is not None and len(self._unknown_detail) < MAX_DEP_REPORT:
                # Diagnosis only (which code produced it); the unknown stays latched.
                self._unknown_detail.setdefault("unrooted-exec:" + filename, set()).add(
                    f"{producer.co_filename}:{producer.co_qualname}")
                self._unknown_producer_source("unrooted-exec:" + filename, producer)
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
        if not _same(code, self._module_compilation(source, code)):
            self.mark_unknown("executed-code-mismatch:" + filename)
            return
        component = "dependency:" + receipt[0]
        self._dep_unit(path, component, receipt[1], "source")
        self._remember(code, component)
        self._dependency_generators(path, code, source)
        self._attrs_generator(path, code, source)

    def _attrs_generator(self, path, code, source):
        """A closed, source-pinned descriptor from the observed module code graph."""
        if (self._deps.identity(path) != _ATTRS_IDENTITY or
                code.co_qualname != "<module>" or _sha(source) != _ATTRS_SOURCE):
            return
        info = self._deps.distribution(path)
        descriptor = GeneratorDescriptor.from_executed(
            "attrs:_compile_and_eval", code, "_compile_and_eval", source,
            {"dist": "attrs", "version": "26.1.0", "record_sha256": info["record_sha256"]})
        descriptor.kind = "attrs"
        descriptor.attrs_codes = {c.co_qualname: c for c in _codes(code)}
        descriptor.record_path = Path(info["dir"]) / (self._deps.rows[path][0] + ".dist-info/RECORD")
        # Bind compile as well as eval to exact AST instruction positions. Unlike
        # exec(source), these are two different CALL sites in the SAME live frame.
        instructions = tuple(dis.get_instructions(descriptor.code, show_caches=True))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "compile":
                position = (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)
                for instruction in instructions:
                    if instruction.opname == "CALL" and tuple(instruction.positions) == position:
                        descriptor.sites[instruction.offset] = "compile"
        self._generators[id(descriptor.code)] = descriptor

    def _attrs_caller(self, frame, descriptor):
        caller = frame.f_back
        if caller is None:
            return None
        name = caller.f_code.co_qualname
        if name == "_linecache_and_compile":
            if caller.f_code is not descriptor.attrs_codes.get(name):
                return None
            caller = caller.f_back
            allowed = _ATTRS_CACHED
        else:
            allowed = _ATTRS_DIRECT
        if caller is None or caller.f_code.co_qualname not in allowed:
            return None
        code = caller.f_code
        self._settle_code(code)
        known = self._known.get(id(code))
        if (code is not descriptor.attrs_codes.get(code.co_qualname) or
                known is None or known[0] is not code):
            return None
        return known[1]

    def _attrs_event(self, event, args, frame, descriptor):
        """A producer receipt alone grants nothing: pair actual output observations."""
        pending = getattr(self._tls, "attrs_output", None)
        self._tls.attrs_output = None  # consume on every event, including failures
        site = descriptor.instruction_sites.get(frame.f_lasti, -1)
        expected = "compile" if event == "compile" else "eval"
        origin = self._attrs_caller(frame, descriptor)
        try:
            path = Path(descriptor.code.co_filename).resolve()
            source = _read_bounded(path, MAX_SOURCE)
            record = _read_bounded(descriptor.record_path, MAX_RECORD_BYTES)
        except (OSError, ValueError):
            self.mark_unknown("attrs-provenance-unreadable")
            return True
        if (descriptor.sites.get(site) != expected or origin is None or
                self._deps.identity(path) != _ATTRS_IDENTITY or
                self._deps.verify(path, source) is None or _sha(source) != _ATTRS_SOURCE or
                _sha(record) != descriptor.distribution["record_sha256"]):
            self.mark_unknown("attrs-provenance-mismatch")
            return True
        script = frame.f_locals.get("script")
        filename = frame.f_locals.get("filename")
        if type(script) is not str or type(filename) is not str or len(script) > MAX_SOURCE:
            self.mark_unknown("attrs-output-unreadable")
            return True
        output = script.encode()
        if len(output) > MAX_SOURCE:
            self.mark_unknown("attrs-output-overflow")
            return True
        if event == "compile":
            if args[0] != output or args[1] != filename:
                self.mark_unknown("attrs-compile-mismatch")
                return True
            self._tls.attrs_output = (frame, output, filename, origin)
            return True
        if pending is None or pending[0] is not frame or pending[1:] != (output, filename, origin):
            self.mark_unknown("attrs-output-receipt-missing")
            return True
        code = args[0]
        # The reviewed attrs module has future annotations; its builtin compile
        # inherits that flag. Reconstruct that exact compilation, not flags=0.
        expected_code = compile(output, filename, "exec", flags=descriptor.code.co_flags & 0x1000000,
                                dont_inherit=True, optimize=sys.flags.optimize)
        if code is not frame.f_locals.get("bytecode") or not _same(code, expected_code):
            self.mark_unknown("attrs-output-code-mismatch")
            return True
        self._record(code, origin, _sha(output), filename=filename, generator=descriptor.name,
                     producer_sha256=descriptor.source_sha256, producer_site=site,
                     producer_distribution=dict(descriptor.distribution))
        return True

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
        info = self._deps.distribution(path)
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
        self._settle_code(frame.f_code)
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
            if self._deps.indexed(path):
                allowed = self._dep_ready() and self._deps.verify(path) is not None
                if not allowed:
                    self.mark_unknown("dependency-unreceipted:" + str(path))
        if caller is None or not allowed:
            reason = "native-load:" + str(name)
            # Loader search is not cwd-relative provenance. Only absolute paths
            # can establish that an unknown library is outside the core root.
            if isinstance(name, str) and os.path.isabs(name):
                self._extension_unknown(reason, name)
            else:
                self.mark_unknown(reason)
            return
        if len(self._native_loads) < MAX_DEP_REPORT:
            self._native_loads.append({"name": name, "caller_component": caller})

    def _unknown_producer_source(self, reason, code):
        """Bind diagnostic attribution to observed source, not a synthetic filename.

        An installer receipt for the producer does not attest its output. Keep
        every unknown and the closed generator table unchanged.
        """
        self._settle_code(code)
        known = self._known.get(id(code))
        if known is None or known[0] is not code:
            return
        source = self._dep_units.get((code.co_filename, "source", None))
        if source is None or source["component"] != known[1]:
            return
        if len(self._unknown_sources) >= MAX_DEP_REPORT and reason not in self._unknown_sources:
            return
        receipts = self._unknown_sources.setdefault(reason, [])
        receipt = {"filename": code.co_filename, "qualname": code.co_qualname,
                   "component": source["component"], "source_sha256": source["sha256"],
                   "trust": "producer source only; generated code remains unknown"}
        if receipt not in receipts and len(receipts) < 8:
            receipts.append(receipt)

    def dependency_receipts(self):
        """Receipts actually used: trusted by installer receipt, not audited, no coverage."""
        with self._lock:
            self._settle()
            return {"trust": DEPENDENCY_TRUST, "roots": [str(r) for r in self._deps.roots],
                    "dists": self._deps.receipts(),
                    "units": [dict(u) for u in self._dep_units.values()],
                    "native_loads": [dict(n) for n in self._native_loads],
                    "unknown_producers": {k: sorted(v) for k, v in self._unknown_detail.items()},
                    "unknown_producer_sources": {k: [dict(r) for r in v]
                                                 for k, v in self._unknown_sources.items()},
                    "generated_trust": {"annotationlib.ForwardRef.evaluate": FORWARDREF_TRUST},
                    "residual_risks": [
                        "RECORD itself is not receipted",
                        "native bytes are hashed before load (TOCTOU); loader-resolved "
                        "transitive libraries are not audited"]}

    def _module_compilation(self, source, code):
        # Admit only repeated module objects, not one-shot import graphs. Identity
        # selects cache policy only: fresh source and exact equality stay mandatory.
        if len(source) > MAX_SOURCE:
            raise ValueError("source-limit")
        known = self._known.get(id(code))
        if known is not None and known[0] is code:
            return self._compiled(source, code.co_filename)
        return _compile(source, code.co_filename)

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

    def defer_comparisons(self):
        """Let this process compare executed module code after it executes (see ``_defer``)."""
        with self._lock:
            self._deferring = True

    def _defer(self, code, producer):
        """Queue one module execution for the comparison ``_observe`` performs.

        Only the time of the comparison moves. The entry keeps the executed code
        object itself, the comparison still reads fresh source bytes, and every
        ledger read settles the queue first: nothing unsettled is ever reported.
        """
        if not isinstance(code, CodeType) or os.getpid() != self._pid:
            self._observe(code, producer)
            return
        self._own(code, self._enqueue(["module", (code, getattr(producer, "f_code", None)), False, None, None]))

    def _own(self, code, entry):
        """Index ``code`` under its newest pending execution.

        An earlier pending execution of the same code object is settled first, so
        executions of one code object are always compared in the order they ran.
        """
        index = self._pending_index
        for nested in _codes(code):
            earlier = index.get(id(nested))
            if earlier is not None and earlier is not entry:
                self._settle_entry(earlier)
            index[id(nested)] = entry

    def _enqueue(self, entry):
        # [kind, payload, settled, result, verdicts]; the payload holds every code
        # reference. ``verdicts`` is {id(code): [code, component or None]} for the
        # callers a pending origin bound to this execution, filled when it settles.
        if len(self._pending) >= MAX_PENDING:
            self._settle()
        self._pending.append(entry)
        if self._settler is None and not self._stopping:
            settler = threading.Thread(
                target=self._settle_slowly, args=(os.getpid(),),
                name="code-attestation-settle", daemon=True)
            settler.start()
            self._settler = settler
            if not self._exit_hooked:
                self._exit_hooked = True
                atexit.register(self._stop_settling)
        return entry

    def _stop_settling(self):
        # Interpreter exit freezes daemon threads wherever they are. A settler frozen
        # inside the ledger lock would hang every audited event of the teardown, so it
        # is stopped and joined while threads still run. What is still owed stays owed.
        with self._lock:
            self._stopping = True
            settler = self._settler
        self._demand.set()
        if settler is not None and settler is not threading.current_thread() and os.getpid() == self._pid:
            settler.join()

    def _settle_under(self, root):
        """Settle pending modules executed from under ``root`` before it becomes a component.

        Their comparison then sees the roots they actually ran under: registration
        after execution stays unrooted, exactly as when compared inline.
        """
        below = os.path.join(str(root), "")
        for entry in tuple(self.__dict__.get("_pending", ())):
            if entry[0] != "module" or entry[2]:
                continue
            if entry[3] is None:
                filename = entry[1][0].co_filename
                try:
                    entry[3] = "" if filename.startswith("<") else str(Path(filename).resolve())
                except (OSError, ValueError):
                    entry[3] = ""
            if entry[3].startswith(below):
                self._settle_entry(entry)

    def _settle_entry(self, entry):
        if entry[2]:
            return
        entry[2] = True
        kind, payload = entry[0], entry[1]
        entry[1] = None
        busy = getattr(self._tls, "busy", False)
        self._tls.busy = True
        try:
            index = self._pending_index
            if kind != "origin":
                for nested in _codes(payload[0] if kind == "module" else payload[2]):
                    if index.get(id(nested)) is entry:
                        del index[id(nested)]
            if kind == "module":
                self._observe(*payload)
            elif kind == "origin":
                entry[3] = self._settled_origin(*payload)
            else:
                self._settle_entry(payload[6])
                self._generated_unit(*payload[:6], payload[6][3])
        except Exception as exc:  # health: allow BLE001 -- diagnostic boundary latches unknown; must never alter observed execution
            self.mark_unknown("audit-error:" + type(exc).__name__)
        except BaseException as exc:
            # The module already ran: an interrupted comparison is unknown, never skipped.
            self.mark_unknown("audit-error:" + type(exc).__name__)
            raise
        finally:
            self._tls.busy = busy
            if entry[4]:
                # What this execution established, read before any later execution
                # of the same code object can be compared.
                known = self._known
                for verdict in entry[4].values():
                    attested = known.get(id(verdict[0]))
                    if attested is not None and attested[0] is verdict[0]:
                        verdict[1] = attested[1]

    def _settle_code(self, code):
        """Settle the pending module that owns ``code`` before its identity is read."""
        # Callers hold the ledger lock. Bare fixtures have no queue.
        entry = self.__dict__.get("_pending_index", {}).get(id(code))
        if entry is not None:
            self._settle_entry(entry)

    def _settle(self, *, deadline=None):
        """Settle every pending comparison in execution order; a deadline keeps the rest pending."""
        # Callers hold the ledger lock. Bare fixtures have no queue.
        pending = self.__dict__.get("_pending")
        if pending and deadline is not None:
            self._demand.set()  # a bounded reader waits on the backlog: stop pacing it
        while pending:
            _check_inventory_deadline(deadline)
            self._settle_entry(pending.popleft())

    def _settle_slowly(self, pid):
        # Background only: bounded share of the interpreter, one comparison per lock hold.
        demand = self._demand
        demand.wait(SETTLE_DELAY)
        while os.getpid() == pid and self._pid == pid:
            began = time.perf_counter()
            with self._lock:
                if not self._pending or self._stopping:
                    self._settler = None
                    demand.clear()  # the next backlog is paced again
                    return
                self._settle_entry(self._pending.popleft())
            if not demand.is_set():
                time.sleep(max(0.001, (time.perf_counter() - began) * (1 / SETTLE_DUTY - 1)))

    def _observe(self, code, producer=None):
        producer = getattr(producer, "f_code", producer)
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
        if not _same(code, self._module_compilation(source, code)):
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
            self._settle_code(code)
            known = self._known.get(id(code))
            if known is not None and known[0] is code:
                return True
            self.mark_unknown("unobserved-code:" + code.co_filename)
            return False

    def executed_units(self):
        with self._lock:
            self._settle()
            return tuple(dict(unit) for unit in self._units)

    def trusted_bootstrap_seed(self):
        return tuple(dict(unit) for unit in self._seed)

    def route_report(self):
        """Route and launcher identity beside the seed; recorded once, sticky."""
        return {"route": self.route, "stub_head_digest": self.stub_head_digest,
                "launcher": dict(self._launcher) if self._launcher else None,
                "seed_required": sorted(SEED_COMMON if self.route else SEED),
                "main_is_seed": False if self.route in STUB_ROUTES else None,
                "interpreter": dict(self.interpreter_identity)}

    def unknowns(self):
        with InventoryObservation(self) as diagnostics:
            self._settle()
            self._inventory_modules(diagnostics=diagnostics)
            return tuple(sorted(self._unknown))

    def integrity_observation(self, *, deadline=None):
        """One locked inventory with its process-bound identity and unfiltered unknowns.

        Consumers must reject pending/unsupported provenance, not manufacture a
        ledger after startup or filter observational unknowns into permission.
        """
        if deadline is not None and self.__dict__.get("_pending"):
            self._demand.set()  # even a reader that is already out of budget is waiting
        _check_inventory_deadline(deadline)
        with InventoryObservation(self, deadline=deadline) as diagnostics:
            self._settle(deadline=deadline)
            _check_inventory_deadline(deadline)
            self._inventory_modules(deadline=deadline, diagnostics=diagnostics)
            _check_inventory_deadline(deadline)
            if getattr(self, '_reviewed_plugins', None):
                from hermes_cli.plugin_review_records import verify_registered
                verify_registered(self)
            _check_inventory_deadline(deadline)
            return {"core_root": str(self.core_root), "boot_sha": self.boot_sha,
                    "route": self.route, "generation": self._generation,
                    "unknowns": tuple(sorted(self._unknown))}

    def latched_unknowns(self):
        """The latched unknowns without a module inventory (cheap: polled while measuring)."""
        with self._lock:
            self._settle()
            return tuple(sorted(self._unknown))

    def generation(self):
        with self._lock:
            self._settle()
            return self._generation


_installed = None


def install(core_root, boot_sha):
    """Install once at the reviewed bootstrap boundary; route is derived HERE.

    The route comes only from ``sys.orig_argv``/``sys.argv``, the ``__main__`` frame
    chain and the boot-tree blobs; no parameter, environment value or ``sys`` flag
    participates. Provenance-pending is cleared only by this function's own path when
    the supported route was verified and the seed inventory is clean. Supported routes: the
    direct managed stub and its ``venv_sync.relaunch_command`` child (``managed-stub-relaunch``,
    proven byte-exact). Any other launch (console, ``-m``, a relaunch differing in any byte,
    ``--run-module``, runtime_command ``-c`` ...) latches a sticky ``unsupported-route:*`` (or
    ``relaunched-launch``) unknown and installs no hook.
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


def defer_comparisons():
    """Opt this process into deferred module comparisons; False when nothing is installed."""
    if _installed is None:
        return False
    _installed.defer_comparisons()
    return True


def _check_inventory_deadline(deadline):
    # Cancellation is advisory-only; never return a partially verified positive.
    # One filesystem operation is not interruptible, so check between modules.
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('code attestation inventory budget exhausted')


def diagnostic_snapshot(*, deadline: float, progress=None) -> 'DiagnosticSnapshot':
    """Bounded metadata-only read of the already installed ledger; never authorizes code."""
    from hermes_cli.code_attestation_snapshot import snapshot
    if progress is None:
        return snapshot(_ledger(), deadline=deadline)
    return snapshot(_ledger(), deadline=deadline, progress=progress)


def integrity_observation(*, deadline=None):
    """Observe only the ledger installed at bootstrap; missing installation refuses."""
    return _ledger().integrity_observation(deadline=deadline)


def unknowns():
    return _ledger().unknowns()


def generation():
    return _ledger().generation()
