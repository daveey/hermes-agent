"""Exact list-shaped sidebar slices for Desktop's cross-gateway merge.

Unlike the native sidebar, these retain exact totals and list source policy.
The caller supplies a request-local annotation snapshot; no results outlive it.
"""


def read_list_slices(reader, snapshot, *, profile, recents_limit, cron_limit,
                     messaging_limit, recents_exclude, messaging_exclude):
    common = dict(profile=(profile or "all").strip() or "all", offset=0,
                  min_messages=1, archived="exclude", order="recent")
    with snapshot():
        return {
            "list_slices_version": 1,
            "recents": reader(**common, limit=recents_limit, exclude_sources=recents_exclude),
            "cron": reader(**common, limit=cron_limit, source="cron"),
            "messaging": reader(**common, limit=messaging_limit, exclude_sources=messaging_exclude),
        }
