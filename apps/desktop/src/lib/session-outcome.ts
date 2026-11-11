/**
 * [daveey] A chat's stored outcome (session-outcome contract): the backend keeps exactly one per non-archived chat
 * (`row.outcome`), and the sidebar paints from it instead of guessing from idleness. This module maps it to the
 * row's status stripe and to the stripe's tooltip. A row without `outcome` (an older backend) maps to nothing, and
 * `sessionStatusStripe` falls back to its old derivation.
 */
import type { Translations } from '@/i18n'
import type { SessionStatusStripe } from '@/store/session-dot-state'
import type { SessionOutcome, SessionOutcomeWaitingOn } from '@/types/session-outcome'

/** The stripe an outcome paints. done → green, decision → red, muted → grey (parked, like ⌘B), waiting / running
 *  → the existing grey "waiting" glyph, unresolved → amber (the `attention` tone). Null for a kind this app does
 *  not know, so the caller falls back to the old logic rather than painting a guess. */
export const outcomeStripe = (outcome: null | SessionOutcome | undefined): null | SessionStatusStripe => {
  switch (outcome?.kind) {
    case 'done':
      return 'done'

    case 'decision':
      return 'stuck'

    case 'muted':
      return 'blocked'

    case 'waiting':

    case 'running':
      return 'waiting'

    case 'unresolved':
      return 'attention'

    default:
      return null
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
>

/** Workflow-only text stays independent of historical artifact acceptance. */
const workflowTooltip = (outcome: null | SessionOutcome | undefined, r: RowCopy): null | string => {
  switch (outcome?.kind) {
    case 'done': {
      const names = (outcome.crons ?? []).map(job => job.label || job.id)
      const summary = outcome.summary?.trim() || r.outcomeDone

      return names.length ? `${summary}\n${r.outcomeCrons(names)}` : summary
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
