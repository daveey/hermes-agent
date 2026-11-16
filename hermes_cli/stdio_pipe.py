"""Quiet CLI stdout cancellation without changing process-wide SIGPIPE behavior."""
from functools import wraps
import os
import sys


class _PipeOutput:
    """Only stdout writes/flushes are cancellation; internal EPIPE still propagates."""

    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def _call(self, method, *args):
        try:
            return getattr(self.stream, method)(*args)
        except BrokenPipeError:
            # Python flushes the original stream again at shutdown. Redirect its
            # descriptor, rather than closing it or changing inherited SIGPIPE.
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), self.stream.fileno())
            raise SystemExit(1) from None

    def write(self, text):
        return self._call("write", text)

    def flush(self):
        return self._call("flush")


def quiet_stdout_pipe(function):
    """Guard text output, including buffered output on normal/argparse exit."""
    @wraps(function)
    def run(*args, **kwargs):
        original = sys.stdout
        if original is None or original.isatty():
            return function(*args, **kwargs)
        output = _PipeOutput(original)
        sys.stdout = output
        try:
            try:
                result = function(*args, **kwargs)
            except SystemExit:
                output.flush()
                raise
            output.flush()
            return result
        finally:
            sys.stdout = original
    return run
