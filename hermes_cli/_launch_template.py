"""Stdlib-only launcher script template.

The managed ``hermes`` launcher script is rendered here so that bootstrap-time code
attestation can render and compare it without importing ``pm`` or anything after the
bootstrap hook. ``hermes_cli._launchers`` depends on this module, never the reverse.
Imports only ``hermes_cli._early_recovery`` (also stdlib-only).
"""

from __future__ import annotations

# ``sys`` attribute a launcher sets before ``import hermes_bootstrap``: pin HERMES_HOME to the
# install's default root once the launch-time repair ran (``hermes_bootstrap._pin_launcher_home``).
PIN_DEFAULT_HOME_FLAG = "_hermes_pin_default_home"
PIN_MARKER = f"sys.{PIN_DEFAULT_HOME_FLAG} = True\n"
ROOT_TOKEN = "@ROOT@"

# A killed move can tear the repair's own code (``hermes_bootstrap``, ``hermes_cli``). With the
# marker still there, a launcher whose checkout import failed runs the copy of the repair that the
# updater published beside the marker (``update_cmd_commit.publish_recovery_closure``) once every
# file hashes to the blob id its manifest names; a missing, torn or foreign closure (or none, from
# an updater predating it) is rebuilt from git's objects at the marker's ``pre`` with the recorded
# git or an absolute PATH entry (never one from the current directory) and verified the same way.
# Stdlib plus that copy only, under the same claim and checkout lock (a live writer still refuses,
# which exits 1 with the repair's own reason), then a relaunch from the restored tree.
_CLOSURE_REPAIR = """\
def _hermes_closure_repair():
    import contextlib, hashlib, io, shutil, subprocess
    from pathlib import Path
    root = Path(__ROOT__)
    git = root / '.git'
    try:
        if git.is_file():
            text = git.read_text(encoding='utf-8-sig').strip()
            git = root / text[7:].strip() if text.startswith('gitdir:') else git
        fields = dict(line.partition('=')[::2] for line in (git / __MARKER__).read_text(encoding='utf-8-sig').splitlines())
    except OSError:
        return
    pre = fields.get('pre', '').strip()
    if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', pre):
        return
    closure = git / __CLOSURE_DIR__ / pre
    files = (*__CLOSURE__, __INIT__)

    def blob_id(data):
        return hashlib.new('sha1' if len(pre) == 40 else 'sha256', b'blob %d\\0' % len(data) + data).hexdigest()

    def verified():
        try:
            listed = dict(line.split(' ', 1)[::-1] for line in (closure / __MANIFEST__).read_text(encoding='utf-8-sig').splitlines())
            return (set(listed) == set(files) and listed[__INIT__] == blob_id(b'')
                    and all(blob_id((closure / rel).read_bytes()) == oid for rel, oid in listed.items()))
        except (OSError, ValueError):
            return False

    if not verified():
        exe = fields.get('git', '').strip()
        if not (os.path.isabs(exe) and os.path.isfile(exe)):
            names = ('git.exe',) if os.name == 'nt' else ('git',)
            exe = next((os.path.join(d, n) for d in os.environ.get('PATH', '').split(os.pathsep) if os.path.isabs(d)
                        for n in names if os.path.isfile(os.path.join(d, n))), '')
        if not exe:
            return

        def git_out(*args):
            done = subprocess.run([exe, '--no-replace-objects', '-C', str(root), *args], capture_output=True, timeout=60,
                                  stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if done.returncode != 0:
                raise OSError(done.stderr)
            return done.stdout

        staging = closure.with_name('.%s.%d.launch' % (pre, os.getpid()))
        try:
            ids = {}
            for entry in git_out('ls-tree', '-z', '--full-tree', pre, '--', *__CLOSURE__).split(b'\\0'):
                meta, _, rel = entry.decode('utf-8', 'replace').partition('\\t')
                if meta.split()[1:2] == ['blob']:
                    ids[rel] = meta.split()[2]
            blobs = {__INIT__: b''}
            for rel in __CLOSURE__:
                blobs[rel] = git_out('cat-file', 'blob', ids[rel])
                if blob_id(blobs[rel]) != ids[rel]:  # an object that inflates to bytes pre never had
                    raise OSError('%s does not hash to %s' % (rel, ids[rel]))
            blobs[__MANIFEST__] = ''.join('%s %s\\n' % (blob_id(data), rel) for rel, data in blobs.items()).encode('utf-8')
            for rel, data in blobs.items():
                (staging / rel).parent.mkdir(parents=True, exist_ok=True)
                (staging / rel).write_bytes(data)
            shutil.rmtree(closure, ignore_errors=True)
            os.replace(staging, closure)
        except (OSError, KeyError, subprocess.SubprocessError):
            pass
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if not verified():
            return
    for name in [n for n in sys.modules.copy() if n.split('.')[0] in ('hermes_cli', 'hermes_bootstrap', 'hermes_constants', 'pm')]:
        del sys.modules[name]
    tree = os.path.normcase(os.path.realpath(root))
    sys.path[:] = [str(closure)] + [p for p in sys.path if p and os.path.normcase(os.path.realpath(p)) != tree
                                    and not os.path.normcase(os.path.realpath(p)).startswith(tree + os.sep)]
    sys.dont_write_bytecode = True
    said = io.StringIO()
    restored = False
    try:
        with contextlib.redirect_stderr(said):
            from hermes_cli import _early_recovery
            restored = _early_recovery.restore_interrupted_pull(root)
    except Exception as exc:
        said.write('hermes: the checkout cannot start after an interrupted `hermes update`, and it was not '
                   'repaired: %s\\n' % (exc,))
    if restored:
        sys.stderr.write('hermes: the checkout could not start after an interrupted `hermes update`; '
                         'repaired it with the recovery code saved before the update.\\n' + said.getvalue())
        _early_recovery.relaunch_after_restore()
    sys.stderr.write(said.getvalue() or 'hermes: the checkout cannot start after an interrupted `hermes update`; '
                     'launch again once the update that owns it finishes.\\n')
    raise SystemExit(1)
"""


def render(root: str, module: str, func: str) -> str:
    # Profile boot repairs shared launchers: their default must stay at the
    # install's dependency root, not whichever profile triggered publication.
    #
    # The launch-time repair of a checkout a killed update left torn (``hermes_bootstrap`` ->
    # ``_early_recovery.restore_interrupted_pull``) runs before ANY other checkout module is
    # imported: a merge killed while writing ``hermes_constants.py`` must not stop every launch
    # before the repair. The default-home pin (which needs ``hermes_constants``) happens inside
    # ``hermes_bootstrap`` right after the repair (``_PIN_DEFAULT_HOME``), and here again for a
    # bootstrap that predates that hook.
    from hermes_cli._early_recovery import (
        INTERRUPTED_PULL_MARKER,
        RECOVERY_CLOSURE,
        RECOVERY_CLOSURE_DIR,
        RECOVERY_CLOSURE_INIT,
        RECOVERY_CLOSURE_MANIFEST,
    )

    closure_repair = (_CLOSURE_REPAIR.replace("__MARKER__", repr(INTERRUPTED_PULL_MARKER))
                      .replace("__CLOSURE_DIR__", repr(RECOVERY_CLOSURE_DIR))
                      .replace("__MANIFEST__", repr(RECOVERY_CLOSURE_MANIFEST))
                      .replace("__INIT__", repr(RECOVERY_CLOSURE_INIT))
                      .replace("__CLOSURE__", repr(RECOVERY_CLOSURE))
                      .replace("__ROOT__", repr(root)))  # last: a path is never re-substituted
    return (
        # The repair's own stdlib, bound before the checkout root goes on sys.path: a stdlib-named
        # file in the tree must not run in its place ahead of the closure's verification. The first
        # line stays the shape gateway.status._BOOTSTRAPS recognises as this launcher (#124318).
        "import os, re, sys\n"
        "import contextlib, hashlib, io, pathlib, shutil, subprocess\n"
        "os.environ.pop('PYTHONHOME', None)\n"
        "os.environ.pop('PYTHONPATH', None)\n"
        f"sys.path.insert(0, {root!r})\n"
        + closure_repair +
        "if sys.argv[1:2] == ['--print-runtime-command']:\n"
        "    sys.dont_write_bytecode = True\n"
        "    from pathlib import Path\n"
        "    try:\n"
        "        from hermes_cli import _early_recovery\n"
        "    except Exception as exc:\n"
        "        if _hermes_closure_repair() or not isinstance(exc, ImportError):\n"
        "            raise\n"
        "        _early_recovery = None  # a tree without the repair: the imports below report real damage\n"
        f"    if _early_recovery is not None and _early_recovery.restore_interrupted_pull(Path({root!r})):\n"
        "        _early_recovery.relaunch_after_restore()\n"
        "    from hermes_constants import get_default_hermes_root\n"
        "    os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or str(get_default_hermes_root())\n"
        "    from hermes_cli._launchers import print_runtime_command\n"
        f"    print_runtime_command(Path({root!r}), sys.argv[2:])\n"
        "    sys.exit(0)\n"
        f"sys.{PIN_DEFAULT_HOME_FLAG} = True\n"
        "try:\n"
        "    import hermes_bootstrap\n"
        "except Exception:\n"
        "    _hermes_closure_repair()\n"
        "    raise\n"
        "if not os.environ.get('HERMES_HOME'):\n"
        "    from hermes_constants import get_default_hermes_root\n"
        "    os.environ['HERMES_HOME'] = str(get_default_hermes_root())\n"
        "if sys.argv[1:2] == ['--run-module']:\n"
        "    import runpy\n"
        "    if len(sys.argv) < 3: sys.exit('hermes: --run-module needs a module')\n"
        "    module = sys.argv.pop(2)\n"
        "    del sys.argv[1]\n"
        "    runpy.run_module(module, run_name='__main__', alter_sys=True)\n"
        "    sys.exit(0)\n"
        f"from {module} import {func}\n"
        "sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"sys.exit({func}())\n"
    )


def relaunch_prefix(root: str, argv: list[str]) -> str:
    """The single-line source ``venv_sync.relaunch_command`` puts before the re-entry body.

    One stdlib-only definition shared by the producer (``venv_sync``) and the bootstrap-time
    attestation, which compares a relaunched child's ``-c`` source to it byte for byte.
    Selects deps before run_path: ``__setitem__`` returns None, so the ``or`` still execs the
    module; registering it first keeps a later import from re-entering prepare_launch under a
    HERMES_HOME the script has not set yet. ``_hermes_relaunched`` marks the child this starts
    (not processes it spawns) for ``venv_sync._relaunch_target``.
    """
    return (
        f"import sys, runpy; sys.path.insert(0, {root!r}); sys.argv = {argv!r}; "
        "sys._hermes_relaunched = True; "
        "import importlib.util as _iu, pathlib as _pl; "
        f"_bp = _pl.Path({root!r}) / 'hermes_bootstrap.py'; "
        "_spec = _iu.spec_from_file_location('hermes_bootstrap', _bp) if _bp.is_file() else None; "
        "_mod = _iu.module_from_spec(_spec) if _spec is not None else None; "
        "(sys.modules.__setitem__('hermes_bootstrap', _mod) or _spec.loader.exec_module(_mod)) "
        "if _mod is not None else None; "
    )


# The fixed start of every relaunch source (the root literal follows it).
RELAUNCH_HEAD = "import sys, runpy; sys.path.insert(0, "


def split_pin_marker(script: str) -> tuple[str, str] | None:
    """Split at the single pin marker: (head, tail) both excluding the marker, else None."""
    if script.count(PIN_MARKER) != 1:
        return None
    head, tail = script.split(PIN_MARKER)
    return head, tail


def normalized_head(head: str, root: str) -> str:
    """The head with the checkout root's literal replaced by ``@ROOT@``."""
    return head.replace(repr(root), ROOT_TOKEN)
