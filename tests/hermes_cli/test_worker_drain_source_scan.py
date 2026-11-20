"""Source scan: every process-spawn seam is either fenced by the worker-drain latch or inventoried.

Run ``python tests/hermes_cli/test_worker_drain_source_scan.py --write`` to regenerate the inventory
after fencing a seam. The inventory is a ratchet, not a proof of completeness:

* ``fenced`` files must still reference the drain fence (``popen_worker`` / ``admit`` / ``fenced``).
* ``pending`` files are known-unfenced spawn sites. A new spawn file, or more spawn sites in a pending
  file, fails the test until the author fences it or consciously adds it to the inventory.
* A pending file that has since been fenced must be promoted, so the pending list only shrinks.
* ``REQUIRED_SEAMS`` names the seams the drain design owes; each must be fenced or explicitly pending.
* ``INCOMPLETE_SEAMS`` names owed seams the scan cannot see (no raw spawn of their own) that hold no
  lease yet. They are written into the inventory so the gap is on record.

The scan resolves ``import subprocess as x`` / ``from subprocess import Popen`` aliases. It does not see
a spawn function passed as a value (``spawn=subprocess.Popen``), ``getattr`` or third-party launchers
other than the MCP SDK names; those are covered only where REQUIRED_SEAMS names them.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / 'hermes_cli' / 'worker_drain_spawn_inventory.json'
SKIP_TOP = {'tests', 'scripts', 'website', 'docs', 'optional-skills', 'skills', 'node_modules', 'ui-tui',
            'packaging', 'benchmarks', 'e2e', 'evals'}
SUBPROCESS_FUNCS = {'Popen', 'run', 'call', 'check_call', 'check_output'}
OS_FUNCS = {'fork', 'forkpty', 'system', 'popen', 'posix_spawn', 'posix_spawnp', 'execv', 'execve', 'execl',
            'execle', 'execlp', 'execlpe', 'execvp', 'execvpe', 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe',
            'spawnv', 'spawnve', 'spawnvp', 'spawnvpe'}
ASYNC_FUNCS = {'create_subprocess_exec', 'create_subprocess_shell', 'subprocess_exec', 'subprocess_shell'}

# Seams the drain design owes, by exact file path or directory prefix. Each must be present in the
# inventory (fenced or pending) or be an explicit INCOMPLETE seam below. Nothing passes by default.
FENCED_BY_THIS_CHANGE = ('agent/shell_hooks.py', 'cron/scheduler.py', 'tools/mcp_tool_transport.py',
                         'tools/computer_use/cua_backend_session.py', 'tools/process_registry.py',
                         'tools/process_registry_survivable.py', 'tools/environments/local.py',
                         'hermes_cli/pty_bridge.py', 'tools/browser_launcher.py', 'hermes_cli/process_identity.py')
REQUIRED_SEAMS = {
    'registry.dispatch': 'tools/registry.py',
    'gateway session worker': 'gateway/session_worker_spawn.py',
    'gateway managed worker spawn': 'gateway/session_managed_worker.py',
    'platform adapters': 'gateway/platforms',
    'web actions': 'hermes_cli/web_routers',
    'mcp stdio': 'tools/mcp_tool_transport.py',
    'cua': 'tools/computer_use/cua_backend_session.py',
    'shell_hooks': 'agent/shell_hooks.py',
    'cron run reservation': 'cron/scheduler.py',
    'survivable fd transport': 'tools/process_registry_survivable.py',
    'process registry': 'tools/process_registry.py',
    'terminal local env': 'tools/environments/local.py',
    'pty': 'hermes_cli/pty_bridge.py',
}
# Owed seams with NO raw spawn call in their own file, so the scan cannot see them, and no drain lease
# either. They are INCOMPLETE, recorded in the inventory, and stay listed until the file carries the
# fence (the test then fails so the entry is removed with a real test of the new fence).
INCOMPLETE_SEAMS = {
    'gateway/session_worker_spawn.py': 'acquire_process runs a caller-supplied spawn callable with no drain lease; '
                                       'its one caller spawns in gateway/session_managed_worker.py (pending)',
}

ROUTERS = {'admit', 'popen_worker', 'admitted_reservation', '_drain_admit'}
SPAWN_MODULES = {'subprocess': SUBPROCESS_FUNCS, 'os': OS_FUNCS, 'pty': {'fork', 'spawn'},
                 'asyncio': ASYNC_FUNCS, 'asyncio.subprocess': ASYNC_FUNCS}
SDK_SPAWNERS = {'stdio_client', 'open_process'}


def _name(node) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''


def _aliases(tree: ast.AST) -> tuple[dict, dict]:
    """(module aliases, imported-function aliases) for the whole file, function-local imports included.

    ``import subprocess as _sp`` -> {'_sp': 'subprocess'}; ``from subprocess import Popen as P`` ->
    {'P': ('subprocess', 'Popen')}. File-wide rather than scoped: a name bound to a spawn module anywhere
    in the file is treated as that module everywhere, which can only over-count.
    """
    mods = {m: m for m in SPAWN_MODULES if '.' not in m}
    funcs: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in SPAWN_MODULES:
                    mods[a.asname or a.name.split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for a in node.names:
                if node.module in SPAWN_MODULES and a.name in SPAWN_MODULES[node.module]:
                    funcs[a.asname or a.name] = (node.module, a.name)
                elif f'{node.module}.{a.name}' in SPAWN_MODULES:  # from asyncio import subprocess as sp
                    mods[a.asname or a.name] = f'{node.module}.{a.name}'
    return mods, funcs


def _is_spawn(node: ast.Call, aliases: tuple[dict, dict] | None = None) -> bool:
    mods, funcs = aliases or ({m: m for m in SPAWN_MODULES if '.' not in m}, {})
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id in SDK_SPAWNERS or fn.id in funcs
    if not isinstance(fn, ast.Attribute):
        return False
    base, name = _name(fn.value), fn.attr
    module = mods.get(base, '') if isinstance(fn.value, ast.Name) else base
    return bool(name in SPAWN_MODULES.get(module, ()) or name in ASYNC_FUNCS or name in SDK_SPAWNERS
                or (name == 'spawn' and base in {'PtyProcess', 'ptyprocess'}))


def _routed_function(fn) -> bool:
    """A function counts as a drain seam if it is decorated ``fenced`` or calls an admission router
    anywhere in its body, nested functions included.

    This is a ratchet heuristic, not a proof: every raw spawn in such a function is counted as routed
    even if a particular spawn runs before the admission, on another branch, or after the lease closed.
    Whether a given spawn holds the lease is established by the seam's own behaviour test.
    """
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if 'fenced' in _name(target):
            return True
    return any(isinstance(n, ast.Call) and _name(n.func) in ROUTERS for n in ast.walk(fn))


def _spawn_sites(tree: ast.AST) -> tuple[int, int]:
    """(direct, routed): raw spawn calls outside / inside a function that admits through the fence."""
    direct = routed = 0
    aliases = _aliases(tree)

    def visit(node, in_seam):
        nonlocal direct, routed
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_seam = in_seam or _routed_function(node)
            if any('fenced' in _name(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list):
                routed += 1  # a ``@fenced`` entry point is a seam even when the spawn is in a helper it calls
        if isinstance(node, ast.Call) and _name(node.func) == 'popen_worker':
            routed += 1  # the fence's own launcher: the spawn callable is passed as a value, not called here
        elif isinstance(node, ast.Call) and _is_spawn(node, aliases):
            if in_seam:
                routed += 1
            else:
                direct += 1
        for child in ast.iter_child_nodes(node):
            visit(child, in_seam)

    visit(tree, False)
    return direct, routed


def scan() -> dict[str, dict]:
    files = subprocess.run(['git', 'ls-files', '*.py'], cwd=REPO, capture_output=True, text=True, encoding='utf-8',
                           check=True).stdout.split()
    out = {}
    for rel in files:
        if rel.split('/')[0] in SKIP_TOP:
            continue
        try:
            direct, routed = _spawn_sites(ast.parse((REPO / rel).read_text(encoding='utf-8-sig')))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        if direct or routed:
            out[rel] = {'direct': direct, 'routed': routed, 'status': 'pending' if direct else 'fenced'}
    return out


def _inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding='utf-8-sig'))


def write() -> None:
    found = scan()
    doc = {'version': 1, 'files': {k: found[k] for k in sorted(found)}, 'incomplete_seams': INCOMPLETE_SEAMS}
    INVENTORY.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n', encoding='utf-8')


def _count(src: str) -> tuple[int, int]:
    return _spawn_sites(ast.parse(src))


def test_scan_resolves_import_aliases():
    assert _count('import subprocess as _sp\n_sp.run(["x"])') == (1, 0)
    assert _count('def f():\n import subprocess as _subprocess\n _subprocess.run(["x"])') == (1, 0)
    assert _count('from subprocess import Popen\nPopen(["x"])') == (1, 0)
    assert _count('from subprocess import check_output as co\nco(["x"])') == (1, 0)
    assert _count('import os as _os\n_os.system("x")') == (1, 0)
    assert _count('from os import execv\nexecv("x", [])') == (1, 0)
    assert _count('from asyncio import subprocess as asp\nasync def f():\n await asp.create_subprocess_exec("x")') == (1, 0)
    assert _count('from asyncio import create_subprocess_exec as cse\nasync def f():\n await cse("x")') == (1, 0)
    # not spawns: same attribute names on unrelated objects, and a plain local called run
    assert _count('import re\nre.run = 1\nclass A:\n def run(self): pass\nA().run()\ndef run(): pass\nrun()') == (0, 0)
    assert _count('import subprocess as sp\ndef f():\n lease = admit("x")\n sp.Popen(["x"])') == (0, 1)
    assert _count('import subprocess\ndef f():\n return popen_worker(subprocess.Popen, ["x"])') == (0, 1)


def test_no_new_or_grown_spawn_site_escapes_inventory():
    inv, found = _inventory()['files'], scan()
    new = sorted(set(found) - set(inv))
    assert not new, f'new subprocess seams not in worker_drain_spawn_inventory.json (fence them): {new}'
    grown = {k: (inv[k]['direct'], v['direct']) for k, v in found.items() if k in inv and v['direct'] > inv[k]['direct']}
    assert not grown, f'spawn sites grew in inventoried files: {grown}'


def test_status_matches_source_so_pending_only_shrinks():
    inv, found = _inventory()['files'], scan()
    promoted = sorted(k for k, v in found.items() if k in inv and inv[k]['status'] == 'pending' and v['status'] == 'fenced')
    demoted = sorted(k for k, v in found.items() if k in inv and inv[k]['status'] == 'fenced' and v['status'] == 'pending')
    assert not demoted, f'fenced seam lost its drain fence: {demoted}'
    assert not promoted, f'now fenced: regenerate the inventory (--write) to promote: {promoted}'
    gone = sorted(set(inv) - set(found))
    assert not gone, f'inventoried file no longer has spawn sites: regenerate: {gone}'


def _has_fence(prefix: str) -> bool:
    """The file (or any file under the directory) imports the drain fence module. An import, not a
    substring: ``admit`` alone also matches ``admitted`` and ``admission``."""
    path = REPO / prefix
    for p in ([path] if path.is_file() else sorted(path.rglob('*.py'))):
        for node in ast.walk(ast.parse(p.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.ImportFrom) and (node.module or '').endswith('worker_drain_fence'):
                return True
            if isinstance(node, ast.ImportFrom) and any(a.name == 'worker_drain_fence' for a in node.names):
                return True
            if isinstance(node, ast.Import) and any(a.name.endswith('worker_drain_fence') for a in node.names):
                return True
    return False


def test_required_seams_are_accounted_for():
    """Every owed seam is in the inventory (fenced or pending) or is a recorded INCOMPLETE seam.

    There is no pass-by-default: a seam with no raw spawn and no fence must be named in
    INCOMPLETE_SEAMS, and that entry is itself checked below.
    """
    doc = _inventory()
    inv = doc['files']
    missing = {}
    for label, prefix in REQUIRED_SEAMS.items():
        assert (REPO / prefix).exists(), f'{label}: {prefix} moved; update REQUIRED_SEAMS'
        inventoried = any(k == prefix or k.startswith(prefix.rstrip('/') + '/') for k in inv)
        if not inventoried and prefix not in INCOMPLETE_SEAMS and prefix != 'tools/registry.py':
            missing[label] = prefix
    assert _has_fence('tools/registry.py'), 'registry dispatch lost its tested admission fence'
    assert not missing, f'required drain seams neither inventoried nor recorded INCOMPLETE: {missing}'
    assert doc.get('incomplete_seams') == INCOMPLETE_SEAMS, 'regenerate the inventory (--write)'


def test_incomplete_seams_are_really_unfenced_and_unscanned():
    """An INCOMPLETE entry is honest only while the file has no raw spawn (else it belongs to the
    scanned inventory) and no fence (else it is done: delete the entry and prove the fence)."""
    found = scan()
    for prefix in INCOMPLETE_SEAMS:
        assert prefix in REQUIRED_SEAMS.values(), prefix
        assert prefix not in found, f'{prefix} now has a raw spawn: inventory it instead'
        assert not _has_fence(prefix), f'{prefix} now carries the drain fence: remove it from INCOMPLETE_SEAMS'


def test_known_unfenced_required_seams_are_pending_not_fenced():
    """The handoff may only call these INCOMPLETE; the inventory must agree."""
    inv = _inventory()['files']
    for prefix in ('gateway/session_managed_worker.py', 'gateway/platforms', 'hermes_cli/web_routers'):
        entries = {k: v for k, v in inv.items() if k == prefix or k.startswith(prefix + '/')}
        assert entries and any(v['status'] == 'pending' for v in entries.values()), prefix


def test_pending_inventory_is_the_honest_incomplete_list():
    """Pending files still hold unfenced direct spawns (bounded helper probes and the long tail)."""
    pending = [k for k, v in _inventory()['files'].items() if v['status'] == 'pending']
    assert pending, 'no pending seams: only claim completion after independent review'


def test_seams_fenced_by_this_change_stay_fenced():
    inv = _inventory()['files']
    for rel in FENCED_BY_THIS_CHANGE:
        assert _has_fence(rel), rel
        assert inv[rel]['routed'] > 0, f'{rel}: no spawn in it goes through the fence any more'


def test_cron_run_registration_is_fenced_by_a_drain_lease():
    text = (REPO / 'cron' / 'scheduler.py').read_text(encoding='utf-8-sig')
    assert "admit('cron.run')" in text and 'lease_scope' in text


if __name__ == '__main__':
    if '--write' not in sys.argv:
        sys.exit('pass --write to regenerate the inventory')
    write()
    print(f'wrote {INVENTORY}')
