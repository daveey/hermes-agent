"""Private-root native adapter for the event-loop scheduler's idle proof only."""
import os
from pathlib import Path

from hermes_cli.annotation_file_events import FileEvents


class PrivateInputEvents(FileEvents):
    """Keep real native waiting, but exclude shared ancestor subscriptions.

    Production deliberately watches all ancestors, including access metadata.
    Such a subscription cannot promise silence when another test changes a
    shared ancestor. Its coverage is tested separately with ordinary FileEvents.
    This fixture accepts just one existing private parent, with no discovery,
    alias, or control semantics; it is not a substitute production adapter.
    """

    def __init__(self, source):
        super().__init__()
        self.source = source

    def replace(self, files, *, directories=(), controls=()):
        assert list(files) == [self.source]
        assert not directories and not controls
        assert self.source.parent.is_dir()
        assert self.source == Path(os.path.realpath(self.source))
        fd = self._open()
        try:
            wd = self._install(fd, self.source.parent)
            assert wd is not None
        except BaseException:
            os.close(fd)
            raise
        old, self._fd = self._fd, fd
        self._names = {wd: {self.source.name}}
        os.close(old)
