"""HOME final application transport; reply loss resolves immutable private result."""
from hermes_cli import kanban_release_application as app, kanban_release_final as final
from hermes_cli import kanban_release_materialize as materialize, kanban_release_replica as replica

_V2_UNAVAILABLE = 'Board owner release application v2 is unavailable; no downgrade or send'


def process(conn, hub, row, own, board):
    from hermes_cli import kanban_release_home as home
    from hermes_cli.kanban_sync_client import HubError
    home._identity(conn, hub, row, own, board)
    final.install(conn)
    # Completed private result first, before mutable capability/owner/comments/TTL.
    # It never promotes from the historical task image or clears a later hold.
    prior = app.verified_application(conn, row['intent_uid'])
    if prior is not None:
        if materialize.verified(conn, row['intent_uid']) is None:
            raise ValueError('HOME application result lacks canonical private certificate')
        return prior

    def supports_v2():
        reader = getattr(hub, 'decision_retain_capability', None)
        capability = reader(board) if callable(reader) else {}
        return (isinstance(capability, dict)
                and type(capability.get('owner_release_application')) is int
                and capability['owner_release_application'] == 2)

    wire = final.saved(conn, row['intent_uid'])
    if wire is None:
        # NEW freeze is gated on present capability; a retained v2 never downgrades.
        v2 = supports_v2()
        proposal = final.prepared.verified_prepared(conn, row['intent_uid'])
        if proposal is not None and proposal['version'] == 2 and not v2:
            raise ValueError(_V2_UNAVAILABLE)
        proof = replica.verified_consumption(conn, home.queue.wire(row)['client_op_id'])
        if proof is None:
            raise ValueError('HOME final application lacks winning private consumption')
        wire = (final.freeze(conn, proof, delivery_uid=row['delivery_uid'], application_version=2)
                if v2 else final.freeze(conn, proof, delivery_uid=row['delivery_uid']))
    # Historical GET of the exact frozen wire needs no present-day capability.
    try:
        value = hub.owner_release_application(board, row['intent_uid'])
    except HubError as exc:
        if exc.status != 404:
            raise
    else:
        home._identity(conn, hub, row, own, board)
        return materialize.materialize(conn, final.validate(value, wire))
    home._identity(conn, hub, row, own, board)
    if wire['proposal']['version'] == 2 and not supports_v2():
        raise ValueError(_V2_UNAVAILABLE)  # no POST, fallback or remint
    # No writer during I/O. Even a rejected/unknown/lost POST has to reconcile the
    # same immutable operation; no new wire is ever created from a missing reply.
    try:
        hub.ops(board, [wire])
    except (OSError, HubError):
        value = hub.owner_release_application(board, row['intent_uid'])
    else:
        value = hub.owner_release_application(board, row['intent_uid'])
    home._identity(conn, hub, row, own, board)
    return materialize.materialize(conn, final.validate(value, wire))
