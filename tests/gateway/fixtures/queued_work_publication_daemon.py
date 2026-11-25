"""Hold a real terminal commit before its completion event is published."""
import os
from pathlib import Path
import runpy

from gateway import session_results
from tests.waiting import wait_until

finish_result = session_results.finish_result


def delayed_finish(*args, **kwargs):
    result = finish_result(*args, **kwargs)
    home = Path(os.environ['HERMES_HOME'])
    if kwargs['row']['payload'].get('text') == 'B_TURN':
        # The database is terminal, but resume still overlays the running claim.
        wait_until(lambda: (home / 'release-b-publication').exists(),
                   timeout=60, what='viewer observed B before completion publication')
    if kwargs['row']['request_id'] == 'A_FOLLOWER_2':
        # No stream lock is held here: the real replay RPC can observe the gap.
        wait_until(lambda: (home / 'release-publication').exists(),
                   timeout=60, what='viewer observed the terminal/publication gap')
    return result


session_results.finish_result = delayed_finish
runpy.run_module('gateway.run', run_name='__main__')
