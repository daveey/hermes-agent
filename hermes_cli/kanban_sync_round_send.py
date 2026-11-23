"""Shared round send policy, including cooperative sends during long pulls."""
from __future__ import annotations

from dataclasses import astuple


def send(live, hub, slug, gateway, mode, result, *, after_pull, before_head, seen=None):
    from hermes_cli import kanban_sync_client as client
    with client.credited(live):
        pushed = client.push(live, hub, slug, gateway, primary=mode == "primary", before_head=before_head, seen=seen)
    result.push = pushed if result.push is None else client.PushResult(*map(sum, zip(astuple(result.push), astuple(pushed))))
    if after_pull:
        from hermes_cli.kanban_sync_attachments import transfer_pending
        transfer_pending(live, hub, slug)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    from hermes_cli.kanban_sync_reserved_key import advance
    try:
        advance(live, hub, slug)
    except (client.HubError, client.SyncUnavailable, OSError) as exc:
        client._log.info("kanban sync: %s reserved-key advance deferred (%s)", slug, exc)
    result.rejection_resolutions += resolve_rejections(live, hub, slug)
