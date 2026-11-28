"""``hermes producer-mode`` — one supported runtime setter for panel external."""
from __future__ import annotations

from typing import Callable


def _run(args) -> int:
    from hermes_cli.serve_producer_mode import cmd_producer_mode
    return cmd_producer_mode(args)


def build_producer_mode_parser(subparsers, *, cmd_producer_mode: Callable | None = None) -> None:
    parser = subparsers.add_parser(
        'producer-mode',
        help='Set installation-local panel producer mode (no serve PID restart)')
    parser.add_argument('--panel-external', required=True, choices=('true', 'false'),
                        help='Same effective mode as HERMES_SERVE_PANEL_EXTERNAL')
    parser.add_argument('--installation', default=None,
                        help='Hermes installation root or profile home (default: root of process Hermes home)')
    parser.set_defaults(func=cmd_producer_mode or _run)
