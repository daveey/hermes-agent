"""Root observer's local-only result inventory for the activated worker account.

Unlike account admission's GC-aware inventory this observer never retires or
ignores a missing registered database: inability to read it remains UNKNOWN.
"""
import hashlib
import json
from pathlib import Path


def paths(root: Path) -> list[Path]:
    from hermes_cli.worker_account_boards import _installation_boards
    found = _installation_boards(root)
    profiles = root / 'profiles'
    try:
        entries = list(profiles.iterdir())
    except FileNotFoundError:
        entries = []
    for profile in entries:
        if profile.is_dir():
            found.extend(_installation_boards(profile))
    directory = root / 'worker-resources' / 'boards'
    try:
        records = list(directory.iterdir())
    except FileNotFoundError:
        records = []
    for record in records:
        if record.suffix != '.json':
            continue
        raw = json.loads(record.read_text(encoding='utf-8-sig'))
        if (not isinstance(raw, str) or not Path(raw).is_absolute()
                or record.stem != hashlib.sha256(raw.encode()).hexdigest()):
            raise ValueError('invalid result board inventory')
        # Keep every record, even a foreign or vanished path: the read-only
        # result gate must account for it, not turn a discovery omission into 0.
        found.append(Path(raw))
    return list(dict.fromkeys(found))
