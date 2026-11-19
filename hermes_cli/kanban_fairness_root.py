"""Expected runtime epochs and bounded root assembly, outside scheduler locks.

Inventory is registered before a consumer serves. A crash between registration
and opening leaves a missing epoch, not an invisible producer. Event envelopes
and individual segment manifests are never rewritten during assembly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tarfile
import uuid

from hermes_cli import kanban_fairness_budget as budget
from hermes_cli import kanban_fairness_segments as segments
from hermes_cli.kanban_fairness_tail import IDENTITY, OPEN

INVENTORY = "runtime-epochs.json"
REQUIRED = "runtime-required.json"
MAX_EPOCHS = 4096
MAX_INVENTORY = 1024 * 1024
MAX_ASSEMBLED = 2 * 1024 * 1024 * 1024


def inventory(root):
    value = json.loads(segments.read_bounded(root / INVENTORY, MAX_INVENTORY))
    if (not isinstance(value, dict) or set(value) != {"version", "epochs"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["epochs"], list) or len(value["epochs"]) > MAX_EPOCHS):
        raise ValueError("invalid runtime inventory")
    result = {}
    for identity in value["epochs"]:
        if not isinstance(identity, dict) or set(identity) != IDENTITY:
            raise ValueError("invalid epoch identity")
        epoch = identity["source_epoch"]
        if not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", epoch) or epoch in result:
            raise ValueError("invalid or duplicate epoch")
        result[epoch] = identity
    return result


def register(root, identity):
    """Durably reserve an epoch before creating its directory or writer thread."""
    with budget._locked(root):
        if (root / REQUIRED).exists() and not (root / INVENTORY).exists():
            raise ValueError("runtime inventory missing; refusing to replace lost evidence")
        if not (root / REQUIRED).exists():
            marker = segments.encoded({"version": 1})
            if budget._used(root) + len(marker) > budget._limit(root):
                raise budget.BudgetExceeded("runtime marker budget exceeded")
            budget._write(root / REQUIRED, marker)
        epochs = inventory(root) if (root / INVENTORY).exists() else {}
        epoch = identity["source_epoch"]
        if epoch in epochs or len(epochs) >= MAX_EPOCHS:
            raise ValueError("runtime epoch inventory full or duplicate")
        data = segments.encoded({"version": 1, "epochs": [*epochs.values(), identity]})
        if len(data) > MAX_INVENTORY or budget._used(root) + len(data) > budget._limit(root):
            raise budget.BudgetExceeded("runtime inventory budget exceeded")
        budget._write(root / INVENTORY, data)


def files(root):
    """Bound all paths, including orphan and temporary bytes; reject special files."""
    if root.is_symlink():
        raise ValueError("capture root must not be a symlink")
    paths, size, count = [], 0, 0

    def walk(directory, depth=0):
        nonlocal size, count
        if depth > 4:
            raise ValueError("capture directory nesting exceeded")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 100000 or entry.is_symlink():
                    raise ValueError("capture path bound or symlink")
                if entry.is_dir(follow_symlinks=False):
                    paths.append(Path(entry.path))
                    walk(Path(entry.path), depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
                    if size > MAX_ASSEMBLED:
                        raise ValueError("assembled capture byte bound exceeded")
                    paths.append(Path(entry.path))
                else:
                    raise ValueError("capture requires regular files")
    walk(root)
    return sorted(paths), size


def collect(root, capture, load_one):
    """Keep discovered but unregistered epochs; missing expected epochs stay gaps."""
    if not any((root / name).exists() for name in
               ("runtime", INVENTORY, REQUIRED, "runtime-arm.json", "runtime-disarmed.json")):
        return capture
    gaps = capture["gaps"]
    try:
        expected = inventory(root)
        if len(expected) == MAX_EPOCHS:
            gaps.append({"code": "runtime_inventory", "detail": "epoch capacity exhausted"})
    except (OSError, ValueError, TypeError, RecursionError):
        expected = {}
        gaps.append({"code": "runtime_inventory"})
    try:
        _, size = files(root)
        if size > budget._limit(root):
            gaps.append({"code": "capture_bound"})
            return capture
    except (OSError, ValueError, TypeError):
        gaps.append({"code": "capture_bound"})
    discovered = set()
    if (root / "runtime").is_dir():
        with os.scandir(root / "runtime") as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    discovered.add(entry.name)
                else:
                    gaps.append({"code": "runtime_inventory"})
    for epoch in sorted(set(expected) | discovered):
        if not re.fullmatch(r"[0-9a-f]{32}", epoch):
            gaps.append({"code": "runtime_inventory"})
            continue
        out = root / "runtime" / epoch
        if epoch not in expected:
            gaps.append({"code": "runtime_inventory"})
        if not out.is_dir():
            gaps.append({"code": "runtime_epoch_missing", "detail": epoch})
            continue
        child = load_one(out)
        try:
            opening = json.loads(segments.read_bounded(out / OPEN, 4096))
            if opening != {"version": 1, **expected[epoch]}:
                raise ValueError("epoch inventory identity mismatch")
            for key in ("capture_id", "board_uid"):
                if capture["manifest"].get(key) not in (None, opening[key]):
                    raise ValueError("root capture identity mismatch")
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            gaps.append({"code": "runtime_inventory"})
        capture["records"].extend(child["records"])
        gaps.extend(child["gaps"])
    capture["gaps"] = segments.dedupe_gaps(gaps)
    return capture


class _BoundedOutput:
    def __init__(self, stream):
        self.stream, self.size = stream, 0

    def write(self, data):
        if self.size + len(data) > MAX_ASSEMBLED:
            raise ValueError("assembled archive byte bound exceeded")
        self.size += len(data)
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()


def _archive(source, paths, stream):
    total = 0
    with tarfile.open(fileobj=_BoundedOutput(stream), mode="w:gz") as bundle:
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                member = tarfile.TarInfo(str(Path(source.name) / path.relative_to(source)))
                member.type, member.mode = tarfile.DIRTYPE, 0o700
                bundle.addfile(member)
                continue
            # Read exactly the admitted size from a regular no-follow descriptor.
            # Growth after fstat is not copied, shrinkage refuses publication.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as data:
                info = os.fstat(data.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("export requires regular files")
                total += info.st_size
                if total > MAX_ASSEMBLED:
                    raise ValueError("assembled capture byte bound exceeded")
                member = tarfile.TarInfo(str(Path(source.name) / path.relative_to(source)))
                member.size, member.mode = info.st_size, 0o600
                bundle.addfile(member, data)


def export(source, target):
    if source.is_symlink() or target.is_symlink():
        raise ValueError("export paths must not be symlinks")
    source, target = source.resolve(), target.resolve()
    if target == source or source in target.parents or target.exists():
        raise ValueError("export requires a new output outside the capture")
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        # Explicit export after disarm avoids holding active writers behind gzip.
        # Missing tails may still be exported, but load() retains their gaps.
        with budget._locked(source):
            if (source / "runtime-arm.json").exists():
                raise ValueError("disarm runtime capture before export")
            paths, _ = files(source)
            with temporary.open("xb") as stream:
                _archive(source, paths, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)  # refuses an output concurrently created by another caller
    finally:
        temporary.unlink(missing_ok=True)
