"""Compatibility entry points for the process fact scheduler. [daveey]

Both consumer APIs use fact_process's store, scoped custody and capture fences;
this module owns no scheduler state.
"""
from hermes_cli.fact_process import FeedLease as ProcessOwner, store as process_store

__all__ = ['ProcessOwner', 'process_store']
