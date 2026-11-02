"""Hold a real terminal commit before its completion event is published."""
import os
from pathlib import Path
import runpy

from gateway import session_results
from tests.waiting import wait_until

finish_result = session_results.finish_result


def delayed_finish(*args, **kwargs):
    result = finish_result(*args, **kwargs)
    if kwargs['row']['request_id'] == 'A_FOLLOWER_2':
        # No stream lock is held here: the real replay RPC can observe the gap.
        wait_until(lambda: (Path(os.environ['HERMES_HOME']) / 'release-publication').exists(),
                   timeout=60, what='viewer observed the terminal/publication gap')
    return result


session_results.finish_result = delayed_finish
runpy.run_module('gateway.run', run_name='__main__')
