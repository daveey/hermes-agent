"""Serve-owned annotation cutover and explicit legacy rollback. [daveey]"""
import asyncio
from contextlib import asynccontextmanager
import os
import sys


@asynccontextmanager
async def lifespan(app):
    from hermes_cli import fact_annotation_boot
    if getattr(app.state, 'annotation_facts', None) is not None:
        raise RuntimeError('annotation startup custody retained')
    # Opt in until asynchronous activation can guarantee non-blocking serve.
    # health: allow HX002 -- creator-defined rollback switch, never a credential
    enabled = os.environ.get('HERMES_FACTS_ANNOTATION', '0').strip().lower() not in ('0', 'off', 'false', 'no')
    if not enabled or not sys.platform.startswith('linux'):
        yield
        return
    def retain(runtime):
        app.state.annotation_facts = runtime
    try:
        await asyncio.to_thread(fact_annotation_boot.prepare, on_created=retain)
        yield
    finally:
        runtime = getattr(app.state, 'annotation_facts', None)
        if runtime is not None:
            await asyncio.to_thread(runtime.close)
            app.state.annotation_facts = None
