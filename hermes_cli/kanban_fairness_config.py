"""Opt-in, profile-scoped fairness evidence settings; parsing performs no IO."""
from dataclasses import dataclass
from pathlib import Path
import re
from collections.abc import Mapping

DEFAULTS = {"enabled": False, "capture_id": None, "directory": None, "max_bytes": 536870912}


@dataclass(frozen=True)
class CaptureConfig:
    capture_id: str
    directory: Path
    max_bytes: int


def resolve(config: Mapping, *, home: Path | None = None) -> CaptureConfig | None:
    """Validate explicit activation; disabled capture creates neither files nor threads."""
    kanban = config.get("kanban", {})
    if not isinstance(kanban, Mapping):
        raise ValueError("kanban must be a mapping")
    raw = kanban.get("fairness_capture", {})
    if not isinstance(raw, Mapping) or set(raw) - DEFAULTS.keys():
        raise ValueError("kanban.fairness_capture must contain only registered settings")
    options = {**DEFAULTS, **raw}
    if type(options["enabled"]) is not bool:
        raise ValueError("fairness_capture.enabled must be a boolean")
    maximum = options["max_bytes"]
    if type(maximum) is not int or not 0 < maximum <= DEFAULTS["max_bytes"]:
        raise ValueError("fairness_capture.max_bytes must be between 1 and 536870912")
    if not options["enabled"]:
        return None
    capture_id = options["capture_id"]
    if not isinstance(capture_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", capture_id):
        raise ValueError("fairness_capture.capture_id must be a bounded safe path component")
    directory = options["directory"]
    if directory is None:
        if home is None:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        directory = home / "kanban" / "evidence" / capture_id
    elif not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("fairness_capture.directory must be an absolute path or null")
    return CaptureConfig(capture_id, Path(directory), maximum)
