# Bounded worker reads and convergence replans

Kanban workers and delegated child agents receive a selective `skill_view` by default: frontmatter,
introductory summary, procedure/rule/safety sections and a section index. Linked files remain listed.
`section="Heading"` expands one indexed section; `section="*"` or `file_path="SKILL.md"` returns the
complete source. Desktop loads, internal unprocessed preloads and background skill-management
review reads remain complete. Tool descriptions and the system-prompt skill index are unchanged.

Dispatch and compact `kanban_show` share a 10,000-character complete-or-expand brief containing the
body, latest checkpoint and five newest comments in full. Governing instructions remain included.
If those inputs exceed the limit, the brief explicitly prohibits execution before a full read; it
never silently cuts acceptance text. Governing instructions are retained in that notice when they
fit, otherwise the notice explicitly requires those too. Parent handoffs and older history have an
explicit expansion pointer. Worker `hermes kanban show` uses the same brief, including `--json`;
`--verbose` expands the CLI view. Interactive CLI show and `hermes kanban context` retain full views.

## Replan prerequisites

Before a ninth real attempt since the beginning or last completed/published replan, HOME creates
one scratch replan prerequisite with `replan:<original>:<episode>` as its idempotency key. The task,
owner/request events, original-card comment and dependency link are one SQLite transaction. The
original waits in `todo`; review-lane waiters resume into review. Both ordinary and review claim
paths recheck the counter before and inside their existing claim transaction. Foreign executors
refuse admission but never create policy work. No new sticky hold/status is introduced.

The replan worker diagnoses non-convergence and records a new plan, unchanged acceptance, or
smaller prerequisites. Acceptance, safety, permissions and budget changes require the original
owner's human-request flow. If a portable worker cannot edit a foreign original, its completion
summary/result carries the actionable plan. The original's HOME appends that handoff to its body
and a comment atomically, preserving the entire prior body, before admission resets the counter.
Publication is idempotent and reads a completed parent's handoff from the normal replica if absent
in the partial live file. A completion marker without a handoff does not reset the counter.
Cancelled/missing/empty-plan replans receive replacement episodes without resetting prior attempts.

Replans carry the normal three-failure breaker and are exempt from recursive auto-replanning.
`max_retries` may raise the convergence threshold; an authenticated release's finite additional-run
mandate also raises it rather than being discarded. Existing no-progress, spend, containment,
permission, review and CI gates remain independent and retain precedence. Replanning grants no
spending or safety authority.

## Deployment acceptance

After ordinary reviewed core landing and deployment, query original request events against actual
parent links and replan rows; a note saying only “replan needed” is not proof. Inspect active cards
beyond the default threshold, distinguishing explicit higher thresholds and independent holds.
Three days after verified deployment, rerun the existing fleet token-audit collectors over the same
worker/session populations and compare skill-load characters per run (at most half the baseline),
brief characters per run (at most 12,000), and replan coverage. Static size comparisons and fixture
success are not substitutes for that delayed production audit.
