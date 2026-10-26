"""Validate the CLI's inherited cross-gateway origin before starting chat."""

import os
import sys

from hermes_cli import kanban_origin


def configure_chat_origin(args):
    """Pin the explicit origin, or validate the inherited process handoff."""
    if getattr(args, "origin_session", None):
        os.environ[kanban_origin.ENV] = str(args.origin_session).strip()
    if kanban_origin.ambient_origin_ref():
        try:
            kanban_origin.resolve()
        except kanban_origin.OriginError as exc:
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            sys.exit(2)
