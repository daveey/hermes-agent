# Overdue home-owned transitions

The hub's pickup-stall sweep also observes the full Board replica for home-owned
transitions. Local pickup being disabled or drained does not disable this pass.
The existing `overdue_wake` per-card diagnostic only renders an explanation; it
cannot file a repair incident on behalf of an offline home.

A scheduled card qualifies when its current canonical wake/backstop is **more than
30 minutes** overdue. A todo card qualifies when all parents are done and the last
parent's completion is more than 30 minutes old. Missing parents, undated completion,
spent or missing parks, terminal cards, and human-only parks do not qualify.

The home must be held, revoked, or have no Board event in the preceding 30 minutes.
A recent event excludes an otherwise live home, but does not override a hold or
revocation. The observer requires a fresh completed replica pull and fresh machine
registry evidence; stale observer state is never evidence of remote silence.
Scans are bounded to once a minute per replica identity.

One open incident is reused per home under the authenticated hub alarm prefix
`pickup-stall:hub:home-overdue-<home>:`. It follows existing pickup-alarm creator,
assignee and placement rules. Repeated passes comment instead of creating a second
card; elapsed age and held-home heartbeats alone do not churn subsequent comments.
A closed incident allows a new incident if the condition persists. Active incidents
are not automatically archived: an agent may still be verifying transfer custody.

Every listed row includes its title, overdue age and latest continuation checkpoint.
Machine-only WIP detection is deliberately conservative: paths or unpublished-work
language require inspection, and no checkpoint is unknown rather than portable.
The incident requests the supported hub-admin batched rehome route, preserving
status, wake time/check, links and comments. The route's own eligibility and custody
checks remain authoritative. This detector never revokes/unholds machines, invents
death proof, or transfers cards itself. A transfer refusal or uncertain custody must
be listed with its reason.

Implementation: `hermes_cli/kanban_pickup_stall_home.py`, called from
`kanban_pickup_stall.sweep`. Tests:
`bash scripts/run_tests.sh tests/hermes_cli/test_kanban_pickup_stall_home.py`.
