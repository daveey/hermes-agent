# Class routing contract (opt-in)

`class:deep`, `class:standard`, and `class:fast` require `provider: auto`.
No class marker reaches a provider wire request. Class routing preserves the
requested class on initial allocation and failure; empty eligible pools fail closed.
Production activation and live entitlement verification are separate from these
implementation tests.

## Policy

The host/profile-scoped `model_routing` section has this version-1 shape:

```yaml
model_routing:
  enabled: false
  policy_version: 1
  allow_metered: false
  enforce_subscription_only: false
  routes:
    deep: []
    standard: []
    fast: []
```

Each entry is an exact mapping:

- `provider`, `model`: exact runtime identifiers, not aliases or class markers.
- `billing`: `subscription`, `credit-funded`, or `metered`.
- `weight`: integer 1–100; all models under a provider in one class use the same
  provider weight. Account count and model count never increase provider share.
- `capabilities`: boolean capabilities (`tools`, `vision`, `streaming`, `json`, …),
  optional positive integer `context_tokens` and `max_latency_ms`.
- `eligible: true`: operator has verified entitlement and exact model availability.
- `funding_verified: true`: operator has verified this billing classification.

Missing attestations fail closed. Subscription labels are accepted only for
`anthropic`, `openai-codex`, and `xai-oauth`. Runtime selection requires an OAuth
credential from the selected native pool and the native endpoint/protocol. Gemini
API credits are `credit-funded`, not subscription/OAuth. Paid/credit routes require
explicit `allow_metered: true` plus verified funding. Core ships no live manifest;
model equivalence and capability limits must be verified, not guessed from names.

When routing is disabled, existing behavior is unchanged. With ordinary class
routing enabled, concrete pins still bypass allocation. With both `enabled: true`
and `enforce_subscription_only: true`, concrete names are NOT spending authority:
OpenRouter, paid API pins, unknown or ambiguous concrete tuples, stale saved
paid overrides and generic paid fallbacks fail closed. This managed mode excludes
all non-subscription routes even if `allow_metered` is true. Configure verified
routes before enabling; migrate durable defaults/generators separately.

Approved concrete native tuples remain exact pins. Their `exact_pin: true` intent
is stored in the allocation transaction and in session metadata, restored across
CLI/Desktop/messaging/API restart and compression, and never gains model failover.
An explicit class switch releases exact intent; a permitted exact switch selects
that tuple even within the same class. Replacement clients and compressor state
are prepared before allocation commit. Preparation failure preserves the previous
live runtime and durable selection. Native pool account refresh remains supported.

Managed send fences validate current policy, selected OAuth credential, actual SDK
endpoint/token/headers, and request identity after Relay processing. Cached clients
without subscription provenance must be recreated; enabling policy does not bless
old credentials. Raw third-party SDK calls outside Hermes are not governed by this
router. No changes to active configuration are implied by installing the code.

## Shared API

`agent.model_routing.resolve_model_route(model, provider='auto', *, config=None,
session_id=None, parent_session_id=None, requirements=None, excluded=(),
state_path=None, eligible=None, prepare=None, exact_pin=False, replace=False)`
returns `{model, provider, metadata}`. A routed request needs a durable session/job
identity. `config` is the full profile config; omitted config uses the effective
profile-scoped loader. `eligible(route)` runs after manifest/capability/billing
filters. `requirements` uses capability booleans, `min_context_tokens` and
`max_latency_ms`; `excluded` contains failed `(provider, model)` pairs.
`prepare(metadata)` runs inside the allocation transaction before commit.

`hermes_cli.runtime_provider.resolve_runtime_provider` accepts
`routing_session_id`, `routing_parent_session_id`, `routing_requirements`,
`routing_config`, `routing_excluded`. Returned runtime includes a concrete model
and `model_routing` metadata. Class requests and managed exact pins cannot enter
the legacy provider fallback ladder. `ModelRoutingError` is deliberately a
ValueError rather than AuthError: callers must not catch it and retry paid discovery.

## Durable state and request identity

`<profile home>/model-routing.sqlite3` stores metadata, alias roots and allocation
counters under `BEGIN IMMEDIATE`. Compression aliases share their parent's route;
resume and repeated tool calls do not allocate again. Failure exclusions are scoped
to a recovery episode, not permanently carried across turns. Same-class failover
prepares credentials/client before commit and never rewrites cached prompt history.

Metadata includes `requested_class`, `policy_version`, `selected_provider`,
`selected_model`, `billing_group`, `reason`, and optional `exact_pin`. No prompts,
credentials or refresh tokens are stored. Account selection remains in native pools.

Chat completions with `X-Hermes-Session-Key` use profile-scoped declared conversation
identity, allowing changing PDF chunks to remain sticky while separate jobs with
identical text stay isolated. Explicit Session-Id takes precedence. Request bodies
remain authoritative: this identity binding does not append stored server history.

## Entry paths and verification boundary

The shared boundary is wired into CLI, Desktop/TUI, messaging (including background
and hygiene), API completions/runs, delegation, cron, generated Kanban workers and
auxiliary sync/async/streaming/direct resolvers. Automatic review/retry Kanban work
retains `class:deep`. Readiness/context probes do not consume durable allocations.
Auxiliary streaming may retry only before the first emitted chunk.

Adjacent tests cover real builders/HTTP ingress/generated worker arguments, native
SDK calls via isolated mock HTTP transports, paid-override zero-send rejection,
capability enforcement, recovery, exact-pin restore, and transactional state.
Mock transports establish implementation behavior, not live entitlement, funding,
quota or fleet deployment. Rollout must separately verify each active profile,
sync/reprovision persistence and eligible provider set. Missing Grok grants or
unverified Gemini funding must be reported rather than called four-provider coverage.
