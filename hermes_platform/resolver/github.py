"""GitHub CLI lookup for service processes with a minimal PATH."""
from pathlib import Path

from hermes_platform.resolver import LookupContext, locate_command


def resolve_gh(path: str | None = None):
    return locate_command("gh", LookupContext(path=path), known_dirs=(
        str(Path.home() / "bin"), str(Path.home() / ".local" / "bin"),
    ))
