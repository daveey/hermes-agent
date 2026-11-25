"""Migrate legacy standalone controllers to a checkout-backed stdlib loader. [daveey]

The backend launcher follows the installed checkout, but early Linux installs copied the controller
and roller beside each other. Updating core and naturally restarting that controller then retained
obsolete drain policy. Update only that known legacy entrypoint, before the normal warm-roll request;
no unit/template rewrite, service action or backend signal belongs here.
"""
from pathlib import Path

from hermes_cli import always_on_dashboard as aod
from hermes_cli._startup_fast import project_root_str

_MARKER = '# Hermes installed-checkout controller loader v1'


def refresh_standalone_controller() -> None:
    entry = Path.home() / '.local/share/hermes-always-on/always_on_dashboard.py'
    if entry.is_symlink():
        raise OSError(f'refusing symlink controller entrypoint: {entry}')
    if not entry.exists():
        return
    previous = entry.read_text(encoding='utf-8-sig')
    if _MARKER not in previous and 'def _interim_main()' not in previous:
        raise OSError(f'unrecognized standalone controller: {entry}')
    # This module is imported from the installed core during the updater's dashboard refresh.
    # Resolve the root, not the interpreter/venv: all three controller actions are stdlib-only.
    root = project_root_str()
    source = f'''{_MARKER}
import argparse
import sys
sys.path.insert(0, {root!r})
from hermes_cli.always_on_dashboard import build_always_on_dashboard_parser
parser = argparse.ArgumentParser()
build_always_on_dashboard_parser(parser.add_subparsers())
args = parser.parse_args(['always-on-dashboard', *sys.argv[1:]])
raise SystemExit(args.func(args))
'''
    if previous != source:
        aod._write_private(entry, source)
