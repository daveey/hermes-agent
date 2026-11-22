"""Post-commit bundle failures keep old client bytes, not old backend code.

Exercise the real POSIX mac_swap function on disposable directories (no macOS
launch or signing claim). Its copy/rename seams deliberately fail; the actual
shell recovery and rollback run. An old client must retain update recovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('failure', ['copy', 'install'])
def test_post_commit_swap_failure_keeps_advanced_code_and_old_complete_bundle(tmp_path: Path, failure: str):
    core = tmp_path / 'core'
    core.mkdir()
    _git(core, 'init', '-q')
    backend = core / 'contract'
    backend.write_text('legacy')
    _git(core, 'add', 'contract')
    _git(core, 'commit', '-qm', 'old contract')
    old = _git(core, 'rev-parse', 'HEAD')
    backend.write_text('fenced session-edit contract')
    _git(core, 'commit', '-qam', 'new contract')
    advanced = _git(core, 'rev-parse', 'HEAD')
    target = tmp_path / 'Applications/Hermes.app'
    rebuilt = core / 'apps/desktop/release/mac-arm64/Hermes.app'
    for app, commit in ((target, old), (rebuilt, advanced)):
        app.mkdir(parents=True)
        (app / 'install-stamp.json').write_text(json.dumps({'commit': commit}))
        (app / 'payload').write_text(commit)

    # Load the production shell functions, not a Python reimplementation of the swap.
    source = (ROOT / 'scripts/desktop-update/posix.sh').read_text()
    functions = source[source.index('mac_bundle_recover()'):source.index('deliver_outcome()')]
    harness = tmp_path / 'swap.sh'
    harness.write_text(
        'set -eu\n' + functions + '\n'
        + f'INSTALL_ROOT={shlex.quote(str(core))}\nRELAUNCH_TARGET={shlex.quote(str(target))}\n'
        + 'FINAL_CODE=0 APP_REBUILD_FAILED=0 DONE_NOTE=""\n'
        + 'log() { printf "%s\\n" "$*"; }\npublish_stage() { :; }\n'
        + 'add_warning() { printf "warning:%s:%s\\n" "$1" "$2"; }\n'
        + ('MAC_DITTO=/usr/bin/false\n' if failure == 'copy' else
           'copy_bundle() { cp -R "$1" "$2"; }\nMAC_DITTO=copy_bundle\n'
           'mv() { if [ "$1" = "$RELAUNCH_TARGET.new" ]; then return 1; fi; command mv "$@"; }\n')
        + 'mac_swap\nprintf "code:%s\\nnote:%s\\n" "$FINAL_CODE" "$DONE_NOTE"\n'
    )
    result = subprocess.run(
        ['/bin/bash', str(harness)], cwd=tmp_path,
        env={**os.environ, 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path)},
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'warning:app-swap:' in result.stdout
    assert 'code:0' in result.stdout
    assert 'Run the update again.' in result.stdout
    assert ('previous app was kept' if failure == 'copy' else 'previous app was restored') in result.stdout
    assert _git(core, 'rev-parse', 'HEAD') == advanced != old
    assert backend.read_text() == 'fenced session-edit contract'
    assert json.loads((target / 'install-stamp.json').read_text())['commit'] == old
    assert (target / 'payload').read_text() == old
    assert not target.with_suffix('.app.old').exists()
    assert not target.with_suffix('.app.new').exists()
