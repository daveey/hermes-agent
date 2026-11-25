"""Private native coverage for scheduler/reader idleness assertions."""
from pathlib import Path

from hermes_cli.annotation_file_events import FileEvents


class PrivateHolderEvents(FileEvents):
    """Retain native descriptors/parser and direct source/control coverage.

    Production ancestor metadata coverage is intentional, but a scheduler test
    cannot attribute another job's ancestor utime to its own source or readers.
    The ordinary FileEvents tests retain ancestor creation/chmod/retarget proof.
    """

    def replace(self, files, *, directories=(), controls=()):
        files, controls = tuple(files), tuple(controls)
        assert not directories
        super().replace(files, controls=controls)
        names, control_names = {}, {}
        for values, target, suffixes in (
            (files, names, ('', '-wal', '-shm', '-journal')),
            (controls, control_names, ('',)),
        ):
            for value in values:
                path = Path(value)
                assert path.is_absolute() and path.parent.is_dir()
                wd = self._install(self._fd, path.parent)
                assert wd is not None
                target.setdefault(wd, set()).update(path.name + suffix for suffix in suffixes)
        self._names, self._controls = names, control_names
