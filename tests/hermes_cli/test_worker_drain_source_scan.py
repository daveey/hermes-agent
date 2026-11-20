"""Source scan: every process-spawn seam is either fenced by the worker-drain latch or inventoried.

Run ``python tests/hermes_cli/test_worker_drain_source_scan.py --write`` to regenerate the inventory
after fencing a seam. The inventory is a ratchet, not a proof of completeness:

* ``fenced`` files must still reference the drain fence (``popen_worker`` / ``admit`` / ``fenced``).
* ``pending`` files are known-unfenced spawn sites. A new spawn file, or more spawn sites in a pending
  file, fails the test until the author fences it or consciously adds it to the inventory.
* A pending file that has since been fenced must be promoted, so the pending list only shrinks.
* ``REQUIRED_SEAMS`` names the seams the drain design owes; each must be fenced or explicitly pending.
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

# Seams the drain design owes, by file path (glob-free exact prefixes). Each must be present in the
# inventory as fenced or pending; "pending" is an honest INCOMPLETE marker, not an exemption.
# Seams with no raw spawn call: they reach a subprocess only through fenced wrappers or other seams.
NO_DIRECT_SPAWN = {'registry.dispatch', 'gateway session worker', 'shell_hooks', 'terminal local env',
                   'survivable fd transport'}
FENCED_BY_THIS_CHANGE = ('agent/shell_hooks.py', 'cron/scheduler.py', 'tools/mcp_tool_transport.py',
                         'tools/computer_use/cua_backend_session.py', 'tools/process_registry.py',
                         'tools/process_registry_survivable.py', 'tools/environments/local.py',
                         'hermes_cli/pty_bridge.py', 'tools/browser_launcher.py', 'hermes_cli/process_identity.py')
REQUIRED_SEAMS = {
    'registry.dispatch': 'tools/registry.py',
    'gateway session worker': 'gateway/session_worker_spawn.py',
    'mcp stdio': 'tools/mcp_tool_transport.py',
    'cua': 'tools/computer_use/cua_backend_session.py',
    'shell_hooks': 'agent/shell_hooks.py',
    'cron run reservation': 'cron/scheduler.py',
    'survivable fd transport': 'tools/process_registry_survivable.py',
    'process registry': 'tools/process_registry.py',
    'terminal local env': 'tools/environments/local.py',
    'pty': 'hermes_cli/pty_bridge.py',
}


ROUTERS = {'admit', 'popen_worker', 'admitted_reservation', '_drain_admit'}


def _name(node) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''


def _is_spawn(node: ast.Call) -> bool:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id in {'stdio_client', 'open_process'}
    if not isinstance(fn, ast.Attribute):
        return False
    base, name = _name(fn.value), fn.attr
    return bool((base == 'subprocess' and name in SUBPROCESS_FUNCS) or (base == 'os' and name in OS_FUNCS)
                or (base == 'pty' and name in {'fork', 'spawn'}) or name in ASYNC_FUNCS
                or name in {'stdio_client', 'open_process'}
                or (name == 'spawn' and base in {'PtyProcess', 'ptyprocess'}))


def _routed_function(fn) -> bool:
    """A function is a drain seam if it is decorated ``fenced`` or itself calls an admission router."""
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if 'fenced' in _name(target):
            return True
    return any(isinstance(n, ast.Call) and _name(n.func) in ROUTERS for n in ast.walk(fn))


def _spawn_sites(tree: ast.AST) -> tuple[int, int]:
    """(direct, routed): raw spawn calls outside / inside a function that admits through the fence."""
    direct = routed = 0

    def visit(node, in_seam):
        nonlocal direct, routed
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_seam = in_seam or _routed_function(node)
        if isinstance(node, ast.Call) and _is_spawn(node):
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
    doc = {'version': 1, 'files': {k: found[k] for k in sorted(found)}}
    INVENTORY.write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n', encoding='utf-8')


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


def test_required_seams_are_accounted_for():
    """Every owed seam is inventoried (fenced or pending), carries the fence, or has a stated reason."""
    inv = _inventory()['files']
    missing = {}
    for label, prefix in REQUIRED_SEAMS.items():
        path = REPO / prefix
        assert path.exists(), f'{label}: {prefix} moved; update REQUIRED_SEAMS'
        if any(k == prefix or k.startswith(prefix.rstrip('/') + '/') for k in inv):
            continue
        text = '\n'.join(p.read_text(encoding='utf-8-sig') for p in ([path] if path.is_file() else path.rglob('*.py')))
        if label in NO_DIRECT_SPAWN and not any(m in text for m in ROUTERS | {'drain_fenced'}):
            continue
        if any(m in text for m in ROUTERS | {'drain_fenced'}):
            continue
        missing[label] = prefix
    assert not missing, f'required drain seams neither inventoried nor fenced: {missing}'


def test_pending_inventory_is_the_honest_incomplete_list():
    """Pending files still hold unfenced direct spawns (bounded helper probes and the long tail)."""
    pending = [k for k, v in _inventory()['files'].items() if v['status'] == 'pending']
    assert pending, 'no pending seams: only claim completion after independent review'


def test_seams_fenced_by_this_change_stay_fenced():
    for rel in FENCED_BY_THIS_CHANGE:
        text = (REPO / rel).read_text(encoding='utf-8-sig')
        assert any(m in text for m in ROUTERS | {'drain_fenced'}), rel


def test_cron_run_registration_is_fenced_by_a_drain_lease():
    text = (REPO / 'cron' / 'scheduler.py').read_text(encoding='utf-8-sig')
    assert "admit('cron.run')" in text and 'lease_scope' in text


if __name__ == '__main__':
    if '--write' not in sys.argv:
        sys.exit('pass --write to regenerate the inventory')
    write()
    print(f'wrote {INVENTORY}')
