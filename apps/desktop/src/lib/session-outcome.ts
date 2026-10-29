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

/** One `on[]` entry in words: a card reads `t_… scheduled · 14:00`, anything else `capacity: hub slot`. */
export const outcomeWaitingLine = (item: SessionOutcomeWaitingOn): string => {
  const head = item.kind === 'card' ? item.id : `${item.kind}: ${item.label || item.id}`
  const state = item.state ? ` ${item.state}` : ''
  const eta = typeof item.eta === 'number' && Number.isFinite(item.eta) ? ` · ${outcomeEtaLabel(item.eta)}` : ''

  return `${head}${state}${eta}`
}

type RowCopy = Pick<
  Translations['sidebar']['row'],
  'outcomeDecision' | 'outcomeDone' | 'outcomeMuted' | 'outcomeRunning' | 'outcomeUnresolved' | 'outcomeWaiting'
>

/** The stripe's tooltip for an outcome, or null when the row has none (old backend, or an unknown kind). */
export const outcomeTooltip = (outcome: null | SessionOutcome | undefined, r: RowCopy): null | string => {
  switch (outcome?.kind) {
    case 'done':
      return outcome.summary?.trim() || r.outcomeDone
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
