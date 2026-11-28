/** Pure v1 work projection. Shared by sidebar and feature-detecting disk plugins.
 * Python counterpart: hermes_cli.execution_observations; both load the golden contract
 * schemas/fixtures/execution-observations-v1.json. Never reads clocks implicitly.
 * Every lookup of producer data is own-property-only and type-checked before any set/map access.
 */
export type ExecutionState =
  'running' | 'queued' | 'waiting' | 'needs_user' | 'done' | 'failed' | 'cancelled' | 'unknown'
export type ObservationSourceClass = 'sidebar' | 'imported_owner'
export interface ExecutionObservation extends Record<string, unknown> {
  state: ExecutionState
  freshness: 'current' | 'unknown' | 'stale'
  unknown_reason?: string
  last_known_state?: unknown
  observed_at?: unknown
  valid_until?: unknown
  last_activity?: unknown
  last_progress?: unknown
}

const STATES: readonly string[] = [
  'running',
  'queued',
  'waiting',
  'needs_user',
  'done',
  'failed',
  'cancelled',
  'unknown'
]
const TTL: Readonly<Record<string, number>> = { sidebar: 180, imported_owner: 600 }
const ID_FIELDS = ['gateway', 'profile', 'board', 'session', 'lineage', 'kind', 'work_id', 'run_id', 'generation']
const PROGRESS_KINDS = ['artifact', 'test_result', 'work_result']
const NONHUMAN_WAIT_KINDS = ['dependency', 'transient', 'time']
const INTERACTIONS = ['approval', 'reply', 'user_terminal', 'physical', 'external']
const WORK_KINDS = ['card', 'turn', 'process', 'subagent']
const PROBE_FIELDS = ['resolved_check', 'resolved_check_every']
// Union of Python str.isspace and JavaScript String.trim sets, so blank/padding checks agree in both languages.
const WS = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'
const EDGE_WS = new RegExp(`^[${WS}]+|[${WS}]+$`, 'g')

const has = (o: object, k: string): boolean => Object.prototype.hasOwnProperty.call(o, k)
const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const object = (v: unknown): Record<string, unknown> => (isObject(v) ? v : {})
const get = (o: unknown, k: string): unknown => (isObject(o) && has(o, k) ? o[k] : undefined)
const trim = (s: string): string => s.replace(EDGE_WS, '')
const text = (v: unknown): v is string => typeof v === 'string' && v.length > 0
const nonblank = (v: unknown): v is string => typeof v === 'string' && trim(v).length > 0
const ident = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v === trim(v)
const codePoints = (s: string): number => [...s].length
const epoch = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 8.64e12
const ttlOf = (c: unknown): number | undefined => (typeof c === 'string' && has(TTL, c) ? TTL[c] : undefined)
const stateOf = (v: unknown): v is ExecutionState => typeof v === 'string' && STATES.includes(v)
const integral = (v: unknown): number | null => (typeof v === 'number' && Number.isSafeInteger(v) && v >= 1 ? v : null)
const attempt = (v: unknown): boolean => integral(v) !== null || ident(v)
const qualified = (v: unknown): boolean => ['gateway', 'profile'].every(k => ident(get(v, k)))

/** Deep copy of JSON-like data (cycle-safe; own keys defined as data so `__proto__` cannot rebind a prototype). */
function copy<T>(value: T, seen = new WeakMap<object, unknown>()): T {
  if (value === null || typeof value !== 'object') {
    return typeof value === 'function' || typeof value === 'symbol' ? (undefined as T) : value
  }
  const known = seen.get(value)

  if (known) {
    return known as T
  }

  if (Array.isArray(value)) {
    const out: unknown[] = []

    seen.set(value, out)
    value.forEach((v, i) => {
      out[i] = copy(v, seen)
    })

    return out as T
  }

  const out: Record<string, unknown> = {}

  seen.set(value, out)

  for (const key of Object.keys(value)) {
    const v = copy((value as Record<string, unknown>)[key], seen)

    if (v !== undefined) {
      Object.defineProperty(out, key, { value: v, enumerable: true, writable: true, configurable: true })
    }
  }

  return out as T
}

function validIdentity(v: unknown): boolean {
  if (!isObject(v)) {
    return false
  }

  const fields = ['gateway', 'profile', 'kind', 'work_id']

  if (get(v, 'kind') === 'card') {
    fields.push('board')
  } else if (!ident(get(v, 'session')) && !ident(get(v, 'lineage'))) {
    return false
  }

  if (['board', 'session', 'lineage'].some(k => has(v, k) && !ident(v[k]))) {
    return false
  }

  const attempts = ['run_id', 'generation'].filter(k => has(v, k))

  return fields.every(k => ident(get(v, k))) && attempts.length > 0 && attempts.every(k => attempt(v[k]))
}

export function executionIdentityKey(v: unknown): string | null {
  if (!validIdentity(v)) {
    return null
  }

  // JSON keeps "7" distinct from 7 and 7.0 equal to 7.
  return JSON.stringify(ID_FIELDS.map(k => [k, get(v, k) ?? null]))
}

/** JSON-semantic equality: bool is not a number; 7 equals 7.0; null is not missing. */
function sameJson(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => sameJson(v, b[i]))
  }

  if (isObject(a) || isObject(b)) {
    if (!isObject(a) || !isObject(b)) {
      return false
    }

    const keys = Object.keys(a)

    return keys.length === Object.keys(b).length && keys.every(k => has(b, k) && sameJson(a[k], b[k]))
  }

  return typeof a === typeof b && a === b
}

function unknown(
  out: ExecutionObservation,
  reason: string,
  freshness: 'unknown' | 'stale' = 'unknown'
): ExecutionObservation {
  return { ...out, state: 'unknown', freshness, unknown_reason: reason }
}

function progress(out: ExecutionObservation, now: number): void {
  const evidence = get(out, 'progress_evidence')
  const at = get(out, 'last_progress')
  const observed = get(out, 'observed_at')
  const kind = get(out, 'last_progress_kind')
  const ref = get(out, 'last_progress_ref')

  const valid =
    epoch(at) &&
    epoch(now) &&
    epoch(observed) &&
    at <= observed &&
    at <= now &&
    typeof kind === 'string' &&
    PROGRESS_KINDS.includes(kind) &&
    nonblank(ref) &&
    isObject(evidence) &&
    get(evidence, 'successful') === true &&
    get(evidence, 'evidence_ref') === ref &&
    epoch(get(evidence, 'observed_at')) &&
    get(evidence, 'observed_at') === at

  if (!valid) {
    out.last_progress = null
    out.last_progress_kind = null
    out.last_progress_ref = null
  }
}

function validWait(value: unknown, now: number): boolean {
  const target = get(value, 'target')

  if (
    !isObject(value) ||
    !qualified(get(value, 'owner')) ||
    !qualified(target) ||
    !ident(get(target, 'kind')) ||
    !ident(get(target, 'work_id'))
  ) {
    return false
  }

  if (get(target, 'kind') === 'card' && !ident(get(target, 'board'))) {
    return false
  }

  const since = get(value, 'since')
  const due = get(value, 'due_at')

  return (
    nonblank(get(value, 'kind')) &&
    nonblank(get(value, 'predicate')) &&
    epoch(since) &&
    since <= now &&
    (due == null || (epoch(due) && due >= since))
  )
}

/** Visible request semantics shared with Python. Probes are stripped before this runs. */
function humanRequest(value: unknown, waitKind?: unknown): boolean {
  if (!isObject(value)) {
    return false
  }

  if (!['action', 'why_human', 'interaction', 'scope', 'evidence'].every(k => nonblank(get(value, k)))) {
    return false
  }

  if (!INTERACTIONS.includes(value.interaction as string)) {
    return false
  }

  if (typeof waitKind === 'string' && NONHUMAN_WAIT_KINDS.includes(waitKind)) {
    return false
  }

  const choices = get(value, 'choices')

  if (choices != null) {
    if (!Array.isArray(choices) || choices.length < 2 || choices.length > 6) {
      return false
    }

    const values = new Set<string>()

    for (const c of choices) {
      const label = get(c, 'label')
      const val = get(c, 'value')

      if (!nonblank(label) || codePoints(trim(label)) > 60 || !nonblank(val) || codePoints(trim(val)) > 120) {
        return false
      }

      values.add(trim(val))
    }

    if (values.size !== choices.length) {
      return false
    }
  }

  return true
}

/** Private probe commands never enter singular or retained visible requests. */
function stripRequest(request: unknown): unknown {
  if (!isObject(request)) {
    return request
  }
  const kept: Record<string, unknown> = {}

  for (const k of Object.keys(request)) {
    if (!PROBE_FIELDS.includes(k)) {
      Object.defineProperty(kept, k, { value: request[k], enumerable: true, writable: true, configurable: true })
    }
  }

  return kept
}

function stripProbes(raw: Record<string, unknown>): Record<string, unknown> {
  const out = { ...raw }

  if (has(raw, 'human_request')) {
    out.human_request = stripRequest(get(raw, 'human_request'))
  }
  const retained = get(raw, 'retained_human_requests')

  if (Array.isArray(retained)) {
    out.retained_human_requests = retained.map(stripRequest)
  }

  return out
}

function envelopeReject(raw: Record<string, unknown>): string | null {
  const version = get(raw, 'version')

  if (!(typeof version === 'number' && version === 1)) {
    return 'legacy_or_unsupported_version'
  }

  if (!validIdentity(get(raw, 'identity'))) {
    return 'missing_qualified_identity'
  }

  if (!qualified(get(raw, 'owner'))) {
    return 'missing_qualified_owner'
  }

  const refs = get(raw, 'evidence_refs')

  if (!Array.isArray(refs) || refs.length === 0 || !refs.every(nonblank)) {
    return 'missing_evidence_references'
  }

  if (!nonblank(get(raw, 'source')) || !nonblank(get(raw, 'source_revision'))) {
    return 'missing_source_identity'
  }

  return null
}

/** Freshness boundary across the envelope and every required source, or the unknown reason. */
function timingBoundary(
  raw: Record<string, unknown>,
  now: number,
  sourceClass: ObservationSourceClass
): { boundary: number } | string {
  const observed = get(raw, 'observed_at')
  const until = get(raw, 'valid_until')
  const ttl = ttlOf(sourceClass)

  if (ttl === undefined || !epoch(now) || !epoch(observed) || !epoch(until)) {
    return 'missing_or_invalid_source_timing'
  }

  if (until < observed || now < observed) {
    return 'clock_inconsistent'
  }

  let boundary = Math.min(until, observed + ttl)
  const sources = has(raw, 'required_sources') ? raw.required_sources : []

  if (!Array.isArray(sources)) {
    return 'invalid_required_sources'
  }

  for (const s of sources) {
    const at = get(s, 'observed_at')
    const end = get(s, 'valid_until')

    if (!epoch(at) || !epoch(end)) {
      return 'missing_required_source_timing'
    }

    if (end < at || now < at) {
      return 'clock_inconsistent'
    }

    // Absent inherits the envelope class; explicit null/array/unknown is invalid.
    const sourceTTL = has(s as object, 'source_class') ? ttlOf((s as Record<string, unknown>).source_class) : ttl

    if (sourceTTL === undefined) {
      return 'invalid_required_source_class'
    }

    boundary = Math.min(boundary, end, at + sourceTTL)
  }

  return { boundary }
}

/** Read-time expiry only, not proof of writer identity or lifecycle binding. */
export function nonRunningReason(value: unknown, now: number): Record<string, unknown> {
  const raw = object(value)
  const at = get(raw, 'observed_at')
  let until = get(raw, 'valid_until')
  let reason = get(raw, 'unknown_reason') || 'dispatcher_evidence_missing'

  if (epoch(at) && epoch(until) && epoch(now) && at <= now && until >= at) {
    const boundary = Math.min(until, at + 180)
    until = boundary

    if (now >= boundary) {
      reason = 'dispatcher_evidence_stale'
    } else if (
      get(raw, 'freshness') === 'current' &&
      ['blocked', 'waiting'].includes(get(raw, 'disposition') as string)
    ) {
      const fields = ['code', 'family', 'actor', 'summary', 'next_action', 'evidence_ref', 'source_revision']

      if (fields.every(k => nonblank(get(raw, k)))) {
        return { ...copy(raw), valid_until: until }
      }
    }
  }

  return {
    code: 'unclassified_refusal',
    family: 'unknown',
    disposition: 'unknown',
    actor: 'unknown',
    summary: 'Execution reason is unknown',
    next_action: 'Inspect the latest authoritative dispatcher evidence',
    freshness: 'unknown',
    unknown_reason: reason,
    observed_at: at ?? null,
    valid_until: until ?? null
  }
}

function freshness(
  source: Record<string, unknown>,
  now: number,
  sourceClass: ObservationSourceClass
): ExecutionObservation {
  const raw = copy(source)

  if (has(raw, 'non_running_reason')) {
    raw.non_running_reason = nonRunningReason(get(raw, 'non_running_reason'), now)
  }
  const state = get(raw, 'state')

  const out: ExecutionObservation = {
    ...raw,
    state: 'unknown',
    freshness: 'unknown',
    observed_at: get(raw, 'observed_at') ?? null,
    valid_until: get(raw, 'valid_until') ?? null,
    last_activity: get(raw, 'last_activity') ?? null,
    last_progress: get(raw, 'last_progress') ?? null,
    age_seconds: null,
    last_known_state: stateOf(state) && state !== 'unknown' ? state : get(raw, 'last_known_state')
  }

  const rejected = envelopeReject(raw)

  if (rejected) {
    return unknown(out, rejected)
  }

  const timing = timingBoundary(raw, now, sourceClass)

  if (typeof timing === 'string') {
    return unknown(out, timing)
  }

  const boundary = timing.boundary
  const observed = get(raw, 'observed_at') as number

  out.valid_until = boundary
  out.age_seconds = now - observed

  if (now >= boundary) {
    return unknown(out, 'source_expired', 'stale')
  }

  if (!stateOf(state)) {
    return unknown(out, 'unsupported_state')
  }

  if (state === 'unknown') {
    const reason = get(raw, 'unknown_reason')

    return unknown(out, text(reason) ? reason : 'source_unknown')
  }

  out.state = state
  out.freshness = 'current'
  delete out.unknown_reason

  return out
}

function receiptValid(receipt: unknown, identity: unknown): boolean {
  const key = executionIdentityKey(identity)

  return (
    key !== null &&
    isObject(receipt) &&
    get(receipt, 'verified') === true &&
    get(receipt, 'disposition') === 'delivered' &&
    nonblank(get(receipt, 'evidence_ref')) &&
    executionIdentityKey(get(receipt, 'identity')) === key
  )
}

function delivered(out: ExecutionObservation): boolean {
  return receiptValid(get(out, 'delivery_receipt'), get(out, 'identity'))
}

/** Typed structural key shared with Python: code-point text, IEEE-754 number bits.
 * Canonical ordering is not evidence authority. Bool differs from number; -0/0 share a key. */
function jsonOrder(value: unknown): string {
  if (value === null) {
    return 'n'
  }

  if (typeof value === 'boolean') {
    return value ? 'b1' : 'b0'
  }

  if (typeof value === 'number') {
    const bytes = new Uint8Array(8)
    new DataView(bytes.buffer).setFloat64(0, value === 0 ? 0 : value)

    return 'd' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  }

  if (typeof value === 'string') {
    return 's' + Array.from(value, c => c.codePointAt(0)!.toString(16).padStart(8, '0')).join('') + ';'
  }

  if (Array.isArray(value)) {
    return 'a[' + value.map(jsonOrder).join('') + ']'
  }

  if (isObject(value)) {
    const keys = Object.keys(value).sort((a, b) =>
      jsonOrder(a) < jsonOrder(b) ? -1 : jsonOrder(a) > jsonOrder(b) ? 1 : 0
    )

    return 'o{' + keys.map(k => jsonOrder(k) + jsonOrder(value[k])).join('') + '}'
  }

  return 'x'
}

function unique(values: unknown[]): unknown[] {
  const byKey = new Map(values.map(v => [jsonOrder(v), v]))

  return [...byKey.keys()].sort().map(k => copy(byKey.get(k)))
}

function array(out: ExecutionObservation, field: string): unknown[] {
  const value = get(out, field)

  return Array.isArray(value) ? value : []
}

function requests(out: ExecutionObservation): unknown[] {
  const retained = array(out, 'retained_human_requests').filter(v => humanRequest(v))

  if (humanRequest(get(out, 'human_request'), waitKind(out))) {
    retained.push(out.human_request)
  }

  return unique(retained)
}

function receipts(out: ExecutionObservation): unknown[] {
  return unique(
    [...array(out, 'historical_delivery_receipts'), get(out, 'delivery_receipt')].filter(v =>
      receiptValid(v, get(out, 'identity'))
    )
  )
}

function preserve(
  out: ExecutionObservation,
  matches: Record<string, unknown>[],
  now: number,
  sourceClass: ObservationSourceClass
): ExecutionObservation {
  if (out.state !== 'unknown') {
    return out
  }
  const projected = matches.map(v => projectExecutionObservation(v, now, sourceClass))
  const asks = unique(projected.flatMap(requests))
  const historical = unique(projected.flatMap(receipts))
  out.retained_human_requests = asks
  out.historical_delivery_receipts = historical

  const fields: [string, unknown[], boolean][] = [
    ['human_request', asks, humanRequest(get(out, 'human_request'), waitKind(out))],
    ['delivery_receipt', historical, delivered(out)]
  ]

  for (const [field, values, valid] of fields) {
    if (!valid) {
      delete out[field]

      if (values.length === 1) {
        out[field] = copy(values[0])
      }
    }
  }

  if (historical.length > 0) {
    out.last_known_state = 'done'
  }

  return out
}

/** Never claim done without a matching verified delivered receipt, whatever the source of the value. */
function lastKnown(out: ExecutionObservation): ExecutionObservation {
  const known = out.last_known_state
  const valid = stateOf(known) && known !== 'unknown' && (known !== 'done' || receipts(out).length > 0)

  out.last_known_state = valid ? known : null

  return out
}

const waitKind = (out: ExecutionObservation): unknown => get(get(out, 'wait'), 'kind')

function project(
  value: unknown,
  now: number,
  sourceClass: ObservationSourceClass,
  expectedIdentity?: unknown
): ExecutionObservation {
  // null and undefined both mean "no fence", as Python's `expected_identity is not None` does.
  if (expectedIdentity !== undefined && expectedIdentity !== null) {
    const expected = executionIdentityKey(expectedIdentity)

    if (expected === null || expected !== executionIdentityKey(get(value, 'identity'))) {
      // Foreign execution fields are not history of the requested execution.
      const out = project({ identity: copy(expectedIdentity) }, now, sourceClass)

      return unknown(out, expected === null ? 'invalid_requested_identity' : 'execution_identity_mismatch')
    }
  }

  const out = freshness(stripProbes(object(value)), now, sourceClass)

  progress(out, now)

  if (out.state === 'unknown') {
    return out
  }

  if (!WORK_KINDS.includes(String(get(out.identity, 'kind')))) {
    return unknown(out, 'unsupported_work_kind')
  }

  if (out.state === 'needs_user' && !humanRequest(out.human_request)) {
    return unknown(out, 'missing_human_request')
  }

  if ((out.state === 'waiting' || out.state === 'needs_user') && !validWait(out.wait, now)) {
    return unknown(out, 'incomplete_wait_condition')
  }

  if (out.state === 'needs_user' && NONHUMAN_WAIT_KINDS.includes(String(waitKind(out)))) {
    return unknown(out, 'invalid_human_wait')
  }

  if (out.state === 'done' && !delivered(out)) {
    return unknown(out, 'unverified_completion')
  }

  return out
}

export function projectExecutionObservation(
  value: unknown,
  now: number,
  sourceClass: ObservationSourceClass = 'sidebar',
  expectedIdentity?: unknown
): ExecutionObservation {
  return lastKnown(project(value, now, sourceClass, expectedIdentity))
}

/** Select within an authoritative current execution. Exact identity fences old runs; same-time conflicts and
 * different sources are unknown rather than ordered by arrival. Malformed entries are ignored; the result is
 * independent of input order. */
export function selectExecutionObservation(
  values: readonly unknown[],
  identity: unknown,
  now: number,
  sourceClass: ObservationSourceClass = 'sidebar'
): ExecutionObservation {
  const key = executionIdentityKey(identity)
  const candidates = Array.isArray(values) ? values : []

  const matches = candidates.filter(
    (v): v is Record<string, unknown> => key !== null && isObject(v) && executionIdentityKey(get(v, 'identity')) === key
  )

  const base = { identity: copy(identity) }

  if (matches.length === 0) {
    return unknown(
      projectExecutionObservation(base, now, sourceClass),
      key === null ? 'invalid_requested_identity' : 'no_observation'
    )
  }

  let out: ExecutionObservation

  if (matches.some(v => !epoch(get(v, 'observed_at')))) {
    out = unknown(projectExecutionObservation(base, now, sourceClass), 'invalid_candidate_timing')
  } else {
    const latestAt = matches.reduce((at, v) => Math.max(at, get(v, 'observed_at') as number), 0)
    const latest = matches.filter(v => get(v, 'observed_at') === latestAt)

    if (
      latest.slice(1).some(v => !sameJson(v, latest[0])) ||
      matches.slice(1).some(v => !sameJson(get(v, 'source'), get(matches[0], 'source')))
    ) {
      out = unknown(projectExecutionObservation(base, now, sourceClass), 'conflicting_source_evidence')
    } else {
      out = projectExecutionObservation(latest[0], now, sourceClass)
    }
  }

  return preserve(out, matches, now, sourceClass)
}

export function summarizeExecutionObservations(
  values: readonly unknown[],
  now: number,
  sourceClass: ObservationSourceClass = 'sidebar'
) {
  const observations = (Array.isArray(values) ? values : []).map(v => projectExecutionObservation(v, now, sourceClass))
  const counts = Object.fromEntries(STATES.map(state => [state, 0])) as Record<ExecutionState, number>
  let outstanding = 0

  for (const observation of observations) {
    counts[observation.state]++

    outstanding += requests(observation).length
  }

  return {
    observations,
    counts,
    outstanding_human_requests: outstanding,
    uncertain: observations.length === 0 || counts.unknown > 0
  }
}
