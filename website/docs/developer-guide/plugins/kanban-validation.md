---
title: "Kanban validation API"
description: "Pure, name-scoped validation for Kanban plugin ingress"
---

# Kanban validation API

The [public plugin API](./public-api.md) publishes individual names from
`hermes_cli.kanban_model_neutral`, not the whole module. These helpers validate
requests before writes; they do not allocate routes, mutate cards, or authorize
execution. Keep routing, history projection and standing-instruction helpers private.

The existing `validate_selector`, `validate_text` and `validate_review` contracts
remain unchanged. The following strict checks are opt-in at local REST update/bulk
and review boundaries, not replacements for create, DB or selfwork admission:

- `validate_explicit_selector(model=None, provider=None, *, mandatory=False)` first
  uses the canonical `validate_selector`, then requires any normalized nonempty
  model to be exactly `class:fast`, `class:standard`, `class:deep` or explicit-only `class:carbon`. It returns
  `(model, 'auto')` for an accepted class and `(None, None)` for an empty selector.
  A concrete provider, a provider without a model, an unknown class, invalid types,
  or a truthy mandatory argument raises `ValueError` with the canonical
  model-neutral error message. Normalization is the existing selector's stripping
  of surrounding whitespace. This API performs no quota or credential lookup.
- `MANDATORY_FLAGS` is the immutable tuple
  `('pin_mandatory', 'model_pin_mandatory', 'require_model')`, the recognized
  top-level update flag names.
- `validate_update_flags(payload)` reads those attributes from a payload object
  (missing attributes default to `None`) and, when its `metadata` attribute is a
  dict, also reads `pin_mandatory` and `require_model` from that dict. Each value
  must be `None`, `False` or the integer `0`; strings, floats, collections and all
  other values raise `ValueError` with the canonical model-neutral error message.
  It returns `None` on success. It neither validates unrelated metadata nor
  mutates the payload. In particular, it does not introduce an additional
  metadata `model_pin_mandatory` contract.

Call `validate_update_flags` before selector/review checks and before any status,
assignment or other consequential mutation. This preserves strict rejection of
non-inert metadata flags even where a downstream mandatory argument uses boolean
truthiness. Catch `ValueError` at the transport boundary and retain the existing
request-error response; these helpers introduce no new wire protocol.

Publication is intentionally name-scoped. `effective_selector`, `routing_view`
and whole-module imports remain private, and the boundary-lint baseline is not
relaxed by this API extension.

## Read-only attention predicate

`hermes_cli.kanban_open_asks:needs_you(card)` returns a boolean for the current
card snapshot. Pass a dictionary with `status`, `block_kind`, `human_request`
(decoded object or stored JSON), and optional projected `wait`, `wakes`, `stale`
and ordered `events`. It rejects terminal/running/stale cards and incomplete
human requests; a historical `block_kind` is never enough. Current projected
human waits and bounded legacy scheduled device-access probes also count as
attention. The result is presentation only: it does not create consent, revive
a retired canonical request, authorize execution or mutate a card. Lanes and
session-pane card rendering use this same predicate. Other module names remain
private.
