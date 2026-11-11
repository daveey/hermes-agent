"""Bounded build-failure logging, without session dictionaries or frame locals."""
import traceback


class BuildFailureTraceback(Exception):
    """Log-only bounded rendering; never retains the original exception or frames."""


def log_build_failure(logger, sid, key, exc):
    # Use a detached diagnostic exception for exc_info. Passing the original
    # exception lets formatters expand arbitrary provider payloads and chains.
    parts = []
    current = exc
    for _ in range(3):
        frames = traceback.extract_tb(current.__traceback__, limit=-20)
        stack = '\n'.join(f'  File "{f.filename}", line {f.lineno}, in {f.name}' for f in frames)
        parts.append(f'{type(current).__name__}: {str(current)[:300]}\n{stack}')
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if current is None:
            break
    diagnostic = BuildFailureTraceback(('Traceback (most recent call last; outer exception first):\n'
                                       + '\nCaused by / during handling:\n'.join(parts))[:8000])
    logger.warning('agent build failed for session %s (%s): %s: %s',
                   sid, key, type(exc).__name__, str(exc)[:300],
                   exc_info=(BuildFailureTraceback, diagnostic, None))
