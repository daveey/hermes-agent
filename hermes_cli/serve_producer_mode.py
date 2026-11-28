"""Installation-local panel producer mode (dormant Window2).

Environment initializes the same effective mode the runtime setter writes.
``HERMES_SERVE_PRODUCERS_EXTERNAL=0`` is a master override to OFF.
Setting an env file alone does not update a running process: OFF serves watch
the revisioned mode file. Do not enable on a real serve from this module.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

PANEL_ENV = 'HERMES_SERVE_PANEL_EXTERNAL'
MASTER_ENV = 'HERMES_SERVE_PRODUCERS_EXTERNAL'
MODE_DIRNAME = 'serve-producers'
MODE_FILENAME = 'panel.mode.json'


def mode_path(installation: Path) -> Path:
    return Path(installation) / MODE_DIRNAME / MODE_FILENAME


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def env_initial_external(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if env.get(MASTER_ENV) == '0':
        return False
    return _truthy(env.get(PANEL_ENV))


def read_mode(installation: Path) -> dict:
    path = mode_path(installation)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'external': False, 'revision': 0}
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('panel mode schema')
    external = payload.get('external')
    revision = payload.get('revision')
    if type(external) is not bool or type(revision) is not int or revision < 0:
        raise ValueError('panel mode schema')
    return {'external': external, 'revision': revision}


def write_mode(installation: Path, *, external: bool, revision: int | None = None) -> dict:
    """Atomically publish one revision of the same panel mode (serialized across processes)."""
    import fcntl

    path = mode_path(installation)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(path.parent / 'panel.mode.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _write_locked(path, installation, external, revision)
    finally:
        os.close(lock_fd)  # releases the flock


def _write_locked(path: Path, installation: Path, external: bool, revision: int | None) -> dict:
    current = read_mode(installation)
    next_revision = current['revision'] + 1 if revision is None else revision
    if next_revision <= current['revision'] and path.exists():
        raise ValueError('mode revision must increase')
    payload = {'external': bool(external), 'revision': int(next_revision)}
    encoded = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    fd, tmp = tempfile.mkstemp(prefix='panel.mode.', dir=str(path.parent))
    try:
        os.write(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return payload


def initialize_mode(installation: Path, environ: dict[str, str] | None = None) -> dict:
    """Process-start selection of the one installation-local mode.

    ``HERMES_SERVE_PRODUCERS_EXTERNAL=0`` forces OFF (publishing a new revision if the stored mode
    was ON, so ON peers drain). An explicit ``HERMES_SERVE_PANEL_EXTERNAL`` initializes the mode;
    an unset variable never overrides a stored revision (the runtime setter stays durable).
    """
    env = os.environ if environ is None else environ
    existing = read_mode(installation)
    if env.get(MASTER_ENV) == '0':
        if existing['revision'] == 0 or existing['external']:
            return write_mode(installation, external=False)
        return existing
    if env.get(PANEL_ENV) is None:
        if existing['revision'] > 0:
            return existing
        return write_mode(installation, external=False, revision=1)
    wanted = env_initial_external(env)
    if existing['revision'] > 0 and existing['external'] == wanted:
        return existing
    return write_mode(installation, external=wanted)


def effective_external(installation: Path, environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if env.get(MASTER_ENV) == '0':
        return False
    stored = read_mode(installation)
    if stored['revision'] > 0:
        return stored['external']
    return env_initial_external(env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='hermes producer-mode',
        description='Set installation-local panel producer mode without restarting serve PIDs.')
    parser.add_argument('--panel-external', required=True, choices=('true', 'false'),
                        help='Same effective mode as HERMES_SERVE_PANEL_EXTERNAL')
    parser.add_argument('--installation', default=None,
                        help='Hermes installation root or profile home (default: root of process Hermes home)')
    return parser


def cmd_producer_mode(args) -> int:
    from hermes_constants import get_default_hermes_root, get_process_hermes_home

    home = Path(args.installation) if args.installation else get_process_hermes_home()
    installation = Path(get_default_hermes_root(home=home))
    state = write_mode(installation, external=(args.panel_external == 'true'))
    print(json.dumps(state, separators=(',', ':'), ensure_ascii=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_producer_mode(args)


if __name__ == '__main__':
    raise SystemExit(main())
