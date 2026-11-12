"""Adopting a rotation parent that grew in the durable store before the compression lease.

Split out of :mod:`agent.conversation_compression` (FILE_LINES ratchet). The origin module imports
:func:`adopt_grown_durable_parent` back as ``_adopt_grown_durable_parent`` for ``_run_summary_phase``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Optional

# The origin module's logger: these lines keep their source in agent.log and in caplog filters.
logger = logging.getLogger("agent.conversation_compression")


def adopt_grown_durable_parent(
    agent: Any, lease: Any, messages: list, verbatim_tail: Optional[list] = None,
) -> Optional[list]:
    """Return the durable parent transcript when it outgrew the in-memory snapshot.
    Rotation only (in-place never loses rows). The snapshot predates the lease: if durable grew, a writer
    committed a turn — ADOPT it (aborting wedged busy sessions forever). Length check only: in-memory edits of
    past turns are legal. Adoption advances ``lease.watermark`` to the snapshot's newest row, so publication
    clones only rows the snapshot does not already carry."""
    if lease.db is None or not lease.sid:
        return None
    durable_loader = getattr(type(lease.db), "get_messages_as_conversation", None)
    if not callable(durable_loader):
        return None
    durable_parent = durable_loader(lease.db, lease.sid)
    if not (isinstance(durable_parent, list) and len(durable_parent) > len(messages)):
        return None
    # In-memory carries this turn's un-persisted user tail; flush it via the normal
    # rotation-boundary path before adopting, else skip adoption (would drop input).
    # The in-memory transcript carries the CURRENT turn's un-persisted user tail (anchored by
    # _persist_user_message_idx) that the durable snapshot read above does not contain yet. Flush that tail
    # through the normal rotation-boundary path (conversation_history = the already-durable prefix, #68196
    # boundary) BEFORE adopting, then re-read the durable parent so the adopted snapshot includes the live
    # input. If the flush fails (or the anchor is unknown), skip adoption entirely: replacing the in-memory
    # transcript with a snapshot that lacks the user's input would silently drop it from the summarized and
    # rotated history (#adopt-live-tail).
    _preflush_idx = getattr(agent, "_persist_user_message_idx", None)
    # No un-persisted tail means the transcript is fully durable: adopting the longer parent cannot drop input.
    _preflush_ok = True
    if isinstance(_preflush_idx, int) and 0 <= _preflush_idx < len(messages):
        _preflush_ok = False
        with contextlib.suppress(Exception):
            _preflush_ok = agent._flush_messages_to_session_db(messages, conversation_history=messages[:_preflush_idx])
    if not _preflush_ok:
        logger.warning(
            "compression: session=%s grew before lease (%d → %d msgs) but the pre-adoption flush of the "
            "live tail failed; skipping durable-snapshot adoption so un-persisted user input is kept",
            lease.sid, len(messages), len(durable_parent),
        )
        return None
    # Re-read after the flush so the adopted snapshot carries the just-persisted tail.
    durable_parent = durable_loader(lease.db, lease.sid, include_row_ids=True)
    if not (isinstance(durable_parent, list) and len(durable_parent) > len(messages)):
        return None
    adopted_row_ids = [rid for m in durable_parent if isinstance(rid := m.pop("_row_id", None), int)]
    # The caller keeps ``verbatim_tail`` out of ``messages`` and rejoins it after the summary: drop its durable
    # copy from the adopted end, else the rejoin folds the tail onto itself ("U\n\nU", stored in the child).
    n_tail = len(verbatim_tail or ())
    if n_tail and [(m.get("role"), m.get("content")) for m in durable_parent[-n_tail:]] == [
            (t.get("role"), t.get("content")) for t in verbatim_tail]:
        durable_parent = durable_parent[:-n_tail]
    if lease.watermark is not None and adopted_row_ids:
        lease.watermark = max(lease.watermark, *adopted_row_ids)
    logger.info(
        "compression: session=%s grew before lease (%d → %d msgs); adopting durable snapshot", lease.sid, len(messages),
        len(durable_parent),
    )
    return durable_parent
