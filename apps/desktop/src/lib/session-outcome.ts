/**
 * [daveey] A chat's stored outcome (session-outcome contract): the backend keeps exactly one per non-archived chat
 * (`row.outcome`), and the sidebar paints from it instead of guessing from idleness. This module maps it to the
 * row's status stripe and to the stripe's tooltip. An outcome that carries the canonical envelope (`outcome.status`)
 * paints `presentation.stripe` and nothing else (`canonicalOutcomeStatus`). A row without `outcome`, or whose stored
 * outcome predates the envelope, is explicitly unknown; it never inherits heuristic green.
 */
import type { Translations } from '@/i18n'
import type { SessionStatusStripe } from '@/store/session-dot-state'
import type { SessionOutcome, SessionOutcomeWaitingOn, SessionStatusEnvelope } from '@/types/session-outcome'

const CANONICAL_STRIPES: readonly string[] = ['attention', 'blocked', 'done', 'external', 'none', 'stuck', 'waiting']
const IDENTITY_KEYS = ['gateway', 'profile', 'session_id', 'owner_epoch'] as const

const isCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0

const isStringList = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(item => typeof item === 'string')

const hasValidPresentation = ({ coverage, kind, presentation }: SessionStatusEnvelope): boolean =>
  Boolean(
    presentation &&
    CANONICAL_STRIPES.includes(presentation.stripe) &&
    isStringList(presentation.reason_codes) &&
    (presentation.stripe !== 'done' || (coverage?.state === 'complete' && kind === 'done'))
  )

/** What a stored outcome's envelope says to paint. `unknown` is an explicit non-green state: the envelope is there
 *  but this app cannot read it (another `status_version`, or malformed), and it never guesses from `kind`. */
export type CanonicalOutcomeStatus =
  | { revision: null; status: 'unknown'; stripe: 'attention' }
  | { revision: number; status: 'canonical'; stripe: null | SessionStatusStripe }

/** [daveey] The canonical status selector every surface (row, dot, tab, stepping) uses. Missing, malformed or unsupported data is unknown.
 *  `status_version` 1 gives `presentation.stripe` verbatim (`none` = no stripe), independent of any client refetch,
 *  work-source completeness, card count or the legacy `kind`. */
export const canonicalOutcomeStatus = (outcome: null | SessionOutcome | undefined): CanonicalOutcomeStatus => {
  const envelope = outcome?.status
  const unknown = { revision: null, status: 'unknown', stripe: 'attention' } as const

  if (envelope === undefined) {
    return unknown
  }

  if (envelope === null || typeof envelope !== 'object') {
    return unknown
  }

  const { coverage, generation, identity, presentation, revision, status_version: version } = envelope

  if (
    version !== 1 ||
    !['done', 'unresolved', 'muted', 'decision', 'waiting', 'running'].includes(envelope.kind ?? '') ||
    !isCount(revision) ||
    !isCount(generation) ||
    !identity ||
    !IDENTITY_KEYS.every(key => typeof identity[key] === 'string' && identity[key].length > 0) ||
    !coverage ||
    !['complete', 'unknown'].includes(coverage.state) ||
    !isStringList(coverage.reasons) ||
    !presentation ||
    !hasValidPresentation(envelope)
  ) {
    return unknown
  }

  if (presentation?.reason_codes.some(reason => ['source_unknown', 'linked_status_unknown'].includes(reason))) {
    return unknown
  }

  return {
    revision,
    status: 'canonical',
    stripe: presentation.stripe === 'none' ? null : (presentation.stripe as SessionStatusStripe)
  }
}

/** Compatibility entry point for diagnostics: the same canonical selector, never legacy kind inference. */
export const outcomeStripe = (outcome: null | SessionOutcome | undefined): null | SessionStatusStripe => {
  return canonicalOutcomeStatus(outcome).stripe
}

/** [daveey] The tone an UNREAD dot takes from the stored outcome. The emerald unread dot means "finished, not yet
 *  seen", and users read it as done; a chat whose outcome is waiting / running / unresolved / decision / muted is not
 *  done, so its unread dot paints that outcome's stripe colour instead (still filled, so it still reads as new).
 *  Null = keep the emerald unread dot (outcome `done`, no outcome, or a kind this app does not know). */
export const unreadDotOutcomeStripe = (outcome: null | SessionOutcome | undefined): null | SessionStatusStripe => {
  const canonical = canonicalOutcomeStatus(outcome)
  const stripe = canonical.stripe

  return stripe && stripe !== 'done' ? stripe : null
}

/** [daveey] The unread dot's label when its outcome is not done: unread, plus what the outcome says (not "Finished"). */
export const unreadDotOutcomeLabel = (
  outcome: SessionOutcome,
  r: Pick<
    Translations['sidebar']['row'],
    'outcomeDecision' | 'outcomeMuted' | 'outcomeRunning' | 'outcomeUnresolved' | 'outcomeWaiting' | 'unreadNotDone'
  >
): string => {
  switch (outcome.kind) {
    case 'decision':
      return r.unreadNotDone(r.outcomeDecision)

    case 'muted':
      return r.unreadNotDone(r.outcomeMuted)

    case 'running':
      return r.unreadNotDone(r.outcomeRunning)

    case 'unresolved':
      return r.unreadNotDone(r.outcomeUnresolved(outcome.reason ?? null))

    default:
      return r.unreadNotDone(r.outcomeWaiting)
  }
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** Local wall-clock `HH:MM` for an epoch-seconds ETA. */
export const outcomeEtaLabel = (eta: number): string => {
  const date = new Date(eta * 1000)

  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

/** A finished chat whose cron keeps running: it draws the loop glyph on the green stripe, not the grey wait. */
export const outcomeHasCrons = (outcome: null | SessionOutcome | undefined): boolean =>
  outcome?.kind === 'done' && Boolean(outcome.crons?.length)

/** [daveey] A finished chat that still watches a card (a later check parked `watch=true`): it keeps the green and
 *  draws the eye glyph, so it never reads as "all done" while the check is pending (self-heal desktop-e6cce5965c). */
export const outcomeHasWatches = (outcome: null | SessionOutcome | undefined): boolean =>
  outcome?.kind === 'done' && Boolean(outcome.watches?.length)

const validEta = (eta: unknown): number | undefined =>
  typeof eta === 'number' && Number.isFinite(eta) ? eta : undefined

/** One `on[]` entry in words: a card reads `t_… scheduled · 14:00`, anything else `capacity: hub slot`. */
export const outcomeWaitingLine = (item: SessionOutcomeWaitingOn): string => {
  const head = item.kind === 'card' ? item.id : `${item.kind}: ${item.label || item.id}`
  const state = item.state ? ` ${item.state}` : ''
  const eta = typeof item.eta === 'number' && Number.isFinite(item.eta) ? ` · ${outcomeEtaLabel(item.eta)}` : ''

  return `${head}${state}${eta}`
}

type RowCopy = Pick<
  Translations['sidebar']['row'],
  | 'outcomeCrons'
  | 'outcomeDecision'
  | 'outcomeDone'
  | 'outcomeMuted'
  | 'outcomeRunning'
  | 'outcomeUnresolved'
  | 'outcomeWaiting'
  | 'outcomeWatches'
>

/** Workflow-only text stays independent of historical artifact acceptance. */
const workflowTooltip = (outcome: null | SessionOutcome | undefined, r: RowCopy): null | string => {
  switch (outcome?.kind) {
    case 'done': {
      const names = (outcome.crons ?? []).map(job => job.label || job.id)

      const watches = outcomeHasWatches(outcome)
        ? (outcome.watches ?? []).map(card => ({ id: card.id, label: card.label, eta: validEta(card.eta) }))
        : []

      const summary = outcome.summary?.trim() || r.outcomeDone

      return [summary, names.length ? r.outcomeCrons(names) : '', watches.length ? r.outcomeWatches(watches) : '']
        .filter(Boolean)
        .join('\n')
    }

    case 'decision': {
      const questions = (outcome.items ?? []).map(item => item.question?.trim()).filter(Boolean)

      return questions.length ? questions.join('\n') : r.outcomeDecision
    }

    case 'muted':
      return r.outcomeMuted

    case 'waiting':
    case 'running': {
      const lines = (outcome.on ?? []).map(outcomeWaitingLine)
      const fallback = outcome.kind === 'running' ? r.outcomeRunning : r.outcomeWaiting

      return lines.length ? lines.join('\n') : fallback
    }

    case 'unresolved':
      return r.outcomeUnresolved(outcome.reason ?? null)

    default:
      return null
  }
}

const qualifiedOwner = /^@(task|session):[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+\/[^\s/@]+$/

// Escape field content, not the structural lines: a scope cannot spoof another evidence/disposition line.
const receiptValue = (value: string): string =>
  JSON.stringify(value)
    .slice(1, -1)
    .replace(/[\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, char => `\\u${char.charCodeAt(0).toString(16)}`)

const receiptSourceTime = (stamp: unknown): string => {
  const date = typeof stamp === 'number' && Number.isFinite(stamp) ? new Date(stamp * 1000) : null

  return date && Number.isFinite(date.getTime()) ? date.toISOString() : 'unspecified'
}

const receiptRevisionFields = (source: NonNullable<SessionOutcome['completion_receipt_source']>): string[] => {
  const parts: string[] = []

  for (const key of ['event_uid', 'run_uid', 'event_id', 'run_id'] as const) {
    const value = source[key]

    if (
      (typeof value === 'string' && value.length <= 2048) ||
      (typeof value === 'number' && Number.isSafeInteger(value))
    ) {
      parts.push(`${key}${key.endsWith('_id') ? ' (store-local)' : ''}: ${receiptValue(String(value))}`)
    }
  }

  return parts
}

const receiptSource = (source: SessionOutcome['completion_receipt_source']): string => {
  if (
    !source ||
    typeof source !== 'object' ||
    Array.isArray(source) ||
    !['task_run', 'session_declaration'].includes(String(source.kind))
  ) {
    return 'unqualified'
  }

  let ref = source.ref

  if (source.kind === 'session_declaration') {
    ref =
      typeof source.profile === 'string' && typeof source.gateway === 'string' && typeof source.session_id === 'string'
        ? `@session:${source.profile}@${source.gateway}/${source.session_id}`
        : null
  }

  if (typeof ref !== 'string' || ref.length > 2048 || !qualifiedOwner.test(ref)) {
    return 'unqualified'
  }

  const stamp = source.kind === 'task_run' ? source.completed_at : source.declared_at
  const parts = [`${receiptValue(ref)} · source_at: ${receiptSourceTime(stamp)}`, ...receiptRevisionFields(source)]

  // The canonical source retains relocation paths and additive fields. Do not dump arbitrary source JSON into UI.
  return parts.join(' · ')
}

type ReceiptItem = NonNullable<SessionOutcome['completion_receipt']>['items'][number]

const text = (value: unknown, limit = 2048): value is string =>
  typeof value === 'string' &&
  Boolean(value.trim()) &&
  value.length <= limit &&
  ![...value].some(char => char.charCodeAt(0) < 32 && char !== '\n' && char !== '\t')

const receiptRemainingValid = (remaining: ReceiptItem['remaining'][number]): boolean =>
  Boolean(
    remaining &&
    text(remaining.scope, 2000) &&
    text(remaining.reason, 2000) &&
    ['pending', 'deferred'].includes(remaining.state) &&
    (remaining.owner === null
      ? remaining.state === 'deferred'
      : text(remaining.owner) && qualifiedOwner.test(remaining.owner))
  )

const receiptEvidenceValid = (evidence: ReceiptItem['evidence'][number]): boolean =>
  Boolean(
    evidence &&
    text(evidence.ref) &&
    text(evidence.observed_at, 64) &&
    ['reported', 'independently_checked'].includes(evidence.verification)
  )

const receiptItemValid = (item: ReceiptItem): boolean =>
  Boolean(
    item &&
    text(item.scope, 2000) &&
    ['delivered', 'superseded', 'contained', 'rejected'].includes(item.disposition) &&
    Array.isArray(item.evidence) &&
    item.evidence.length &&
    item.evidence.length <= 32 &&
    Array.isArray(item.remaining) &&
    item.remaining.length <= 32 &&
    (item.disposition === 'superseded' ? text(item.superseded_by) : item.superseded_by === undefined)
  )

/** Render contract field names verbatim, like qualified IDs, not translated status labels.
 * This is historical evidence text, never a renderer-side admission or freshness check.
 * Wire data may come from an older/foreign backend: malformed data must not crash the row.
 */
const receiptTooltip = (outcome: SessionOutcome): string | null => {
  const receipt = outcome.completion_receipt

  if (
    !receipt ||
    receipt.version !== 1 ||
    !Array.isArray(receipt.items) ||
    !receipt.items.length ||
    receipt.items.length > 32
  ) {
    return null
  }

  const lines = [
    `completion_receipt: historical · completion_receipt_source: ${receiptSource(outcome.completion_receipt_source)}`
  ]

  for (const item of receipt.items) {
    if (!receiptItemValid(item)) {
      return null
    }

    lines.push(`${item.disposition}: ${receiptValue(item.scope)}`)

    for (const evidence of item.evidence) {
      if (!receiptEvidenceValid(evidence)) {
        return null
      }

      lines.push(
        `  evidence: ${evidence.verification} · ${receiptValue(evidence.observed_at)} · ${receiptValue(evidence.ref)}`
      )
    }

    if (item.superseded_by) {
      lines.push(`  superseded_by: ${receiptValue(item.superseded_by)}`)
    }

    for (const remaining of item.remaining) {
      if (!receiptRemainingValid(remaining)) {
        return null
      }

      // Only pending owners are admitted against live ownership; a deferred owner is an unverified reference.
      const ownerLabel = remaining.state === 'deferred' && remaining.owner !== null ? 'owner (unverified)' : 'owner'

      lines.push(
        `  remaining ${remaining.state}: ${receiptValue(remaining.scope)} · ${ownerLabel}: ${receiptValue(String(remaining.owner))} · ${receiptValue(remaining.reason)}`
      )
    }
  }

  const rendered = lines.join('\n')

  return rendered.length <= 65536 ? rendered : null
}

/** The existing tooltip adds historical scoped claims without changing stripe precedence. */
export const outcomeTooltip = (outcome: null | SessionOutcome | undefined, r: RowCopy): null | string => {
  const workflow = workflowTooltip(outcome, r)

  if (workflow === null || !outcome) {
    return workflow
  }

  const receipt = receiptTooltip(outcome)

  return receipt ? `${workflow}\n${receipt}` : workflow
}
