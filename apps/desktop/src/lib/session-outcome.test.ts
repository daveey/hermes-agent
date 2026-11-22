import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TRANSLATIONS } from '@/i18n/catalog'
import { sessionStatusStripe } from '@/store/session-dot-state'
import { canonicalOutcome } from '@/test/canonical-outcome'
import type { SessionOutcome } from '@/types/session-outcome'

import {
  canonicalOutcomeStatus,
  outcomeEtaLabel,
  outcomeHasCrons,
  outcomeHasWatches,
  outcomeStripe,
  outcomeTooltip,
  outcomeWaitingLine,
  unreadDotOutcomeLabel,
  unreadDotOutcomeStripe
} from './session-outcome'

const r = TRANSLATIONS.en.sidebar.row

const OUTCOMES: Record<string, SessionOutcome> = {
  done: { kind: 'done', source: 'declared', summary: 'Shipped PR #12 and closed the card.' },
  decision: {
    kind: 'decision',
    items: [
      {
        id: 'us_1',
        question: 'Merge PR #12?',
        choices: [
          { label: 'Yes', value: 'yes' },
          { label: 'No', value: 'no' }
        ]
      },
      { id: 'card:t_9', question: 'Rent a GPU for the eval?', card_id: 't_9', state: 'open' }
    ]
  },
  muted: { kind: 'muted' },
  waiting: {
    kind: 'waiting',
    on: [
      { kind: 'card', id: 't_abc', state: 'scheduled', eta: 1_793_300_000 },
      { kind: 'capacity', id: 'hub', label: 'hub slot' }
    ]
  },
  running: { kind: 'running', on: [{ kind: 'subagent', id: 'sa_1', label: 'review lane', state: 'running' }] },
  unresolved: { kind: 'unresolved', reason: 'notice_tail' }
}

// Every input the old derivation reads, in the combinations that give each of its answers.
type Inputs = Parameters<typeof sessionStatusStripe>

const OLD_LOGIC_CASES: Inputs[] = [
  ['idle', true],
  ['idle', false],
  ['unread', true],
  ['draft', false],
  ['background', true],
  ['working', true],
  ['stalled', true],
  ['needs-input', true],
  ['idle', true, true],
  ['idle', true, false, 1],
  ['idle', true, false, 0, true],
  ['idle', true, false, 0, false, 'stuck'],
  ['idle', true, false, 0, false, 'parked'],
  ['idle', true, false, 0, false, null, 1],
  ['background', true, false, 0, false, null, 0, 2],
  ['background', true, false, 0, false, null, 0, 2, 1],
  ['working', true, false, 1, false, 'stuck', 1, 2, 1]
]

describe('sessionStatusStripe without a protocol', () => {
  it.each(OLD_LOGIC_CASES)('is unknown instead of deriving a color from %j', (...inputs) => {
    expect(sessionStatusStripe(...inputs)).toBe('attention')

    for (const outcome of Object.values(OUTCOMES)) {
      expect(sessionStatusStripe(inputs[0], inputs[1], false, 0, false, null, 0, 0, 0, outcome)).toBe('attention')
    }
  })
})

describe('outcomeTooltip', () => {
  it('done shows the summary', () => {
    expect(outcomeTooltip(OUTCOMES.done, r)).toBe('Shipped PR #12 and closed the card.')
    expect(outcomeTooltip({ kind: 'done' }, r)).toBe('Done')
  })

  it('decision lists the questions, one per line', () => {
    expect(outcomeTooltip(OUTCOMES.decision, r)).toBe('Merge PR #12?\nRent a GPU for the eval?')
    expect(outcomeTooltip({ kind: 'decision', items: [] }, r)).toBe('Waiting on your decision')
  })

  it('muted says so', () => {
    expect(outcomeTooltip(OUTCOMES.muted, r)).toBe('Muted')
  })

  it('waiting and running list what they wait on', () => {
    expect(outcomeTooltip(OUTCOMES.waiting, r)).toBe(
      `t_abc scheduled · ${outcomeEtaLabel(1_793_300_000)}\ncapacity: hub slot`
    )
    expect(outcomeTooltip(OUTCOMES.running, r)).toBe('subagent: review lane running')
    expect(outcomeTooltip({ kind: 'waiting' }, r)).toBe('Waiting')
    expect(outcomeTooltip({ kind: 'running', on: [] }, r)).toBe('Running')
  })

  it.each([
    ['notice_tail', 'Unresolved: a card update arrived and nothing answered it'],
    ['no_summary', 'Unresolved: the last reply did not wrap the work up'],
    ['turn_failed', 'Unresolved: the last turn failed'],
    ['interrupted', 'Unresolved: the last turn was interrupted'],
    ['subagent_killed', 'Unresolved: a subagent was stopped before it finished'],
    ['subagent_lost', 'Unresolved: a subagent went silent without finishing'],
    ['empty', 'Unresolved: no messages yet'],
    [undefined, 'Unresolved']
  ])('unresolved{%s} gives the reason in words', (reason, text) => {
    expect(outcomeTooltip({ kind: 'unresolved', reason }, r)).toBe(text)
  })

  it('nothing for no outcome or an unknown kind', () => {
    expect(outcomeTooltip(undefined, r)).toBeNull()
    expect(outcomeTooltip(null, r)).toBeNull()
    expect(outcomeTooltip({ kind: 'from-the-future' }, r)).toBeNull()
  })

  it('formats the ETA as local HH:MM', () => {
    const eta = new Date(2026, 9, 29, 14, 5).getTime() / 1000

    expect(outcomeEtaLabel(eta)).toBe('14:05')
    expect(outcomeWaitingLine({ kind: 'card', id: 't_1', state: 'scheduled', eta })).toBe('t_1 scheduled · 14:05')
  })
})

type ReceiptDisposition = NonNullable<SessionOutcome['completion_receipt']>['items'][number]['disposition']

const receiptOutcome = (disposition: ReceiptDisposition = 'delivered', kind = 'done'): SessionOutcome => ({
  kind,
  summary: 'Bounded assessment completed.',
  ...{
    completion_receipt: {
      version: 1,
      items: [
        {
          scope: 'assessed artifact',
          disposition,
          evidence: [
            { ref: 'https://example.com/evidence/1', observed_at: '2026-10-01T12:00:00Z', verification: 'reported' }
          ],
          remaining: [],
          ...(disposition === 'superseded' ? { superseded_by: '@task:default@remote/t_successor' } : {})
        }
      ]
    },
    completion_receipt_source: {
      kind: 'task_run',
      ref: '@task:default@source/t_example',
      event_uid: 'event-source-1',
      run_uid: 'run-source-1',
      completed_at: 1790856000
    }
  }
})

describe('completion receipt tooltip projection', () => {
  it('unknown source coverage cannot accompany a green canonical stripe', () => {
    const malformed = canonicalOutcome('done', { coverage: { state: 'unknown', reasons: ['source_failed'] } })
    expect(canonicalOutcomeStatus(malformed)).toEqual({ status: 'unknown', stripe: 'attention', revision: null })
  })

  it('legacy workflow kinds are explanation, not a second paint classifier', () => {
    expect(outcomeStripe({ kind: 'done' })).toBe('attention')
  })

  it('a malformed canonical kind cannot contradict a green stripe', () => {
    const malformed = JSON.parse(JSON.stringify(canonicalOutcome('done')))
    malformed.status.kind = 'unresolved'
    expect(canonicalOutcomeStatus(malformed)).toEqual({ status: 'unknown', stripe: 'attention', revision: null })
  })

  it.each(['delivered', 'superseded', 'contained', 'rejected'] as const)(
    'renders scoped %s without changing workflow status',
    disposition => {
      const outcome = receiptOutcome(disposition)
      const text = outcomeTooltip(outcome, r)!
      expect(text).toContain(`${disposition}: assessed artifact`)
      expect(text).toContain('reported')
      expect(text).toContain('2026-10-01T12:00:00Z')
      expect(text).toContain('https://example.com/evidence/1')
      expect(text).toContain('@task:default@source/t_example')
      expect(text).toContain('event-source-1')
      expect(text).toContain('run-source-1')
      expect(text).not.toContain('independently_checked')
      expect(outcomeStripe(outcome)).toBe('attention') // receipt prose is not a published status protocol

      if (disposition === 'superseded') {
        expect(text).toContain('@task:default@remote/t_successor')
      }
    }
  )

  it.each(['running', 'waiting', 'decision', 'muted', 'unresolved'])(
    'retains historical provenance while current kind is %s',
    kind => {
      const outcome = receiptOutcome('rejected', kind)
      const text = outcomeTooltip(outcome, r)!
      expect(text).toContain('completion_receipt')
      expect(text).toContain('rejected: assessed artifact')
      expect(text).toContain('event-source-1')
      expect(outcomeStripe(outcome)).toBe(outcomeStripe({ kind }))
      expect(outcomeTooltip(JSON.parse(JSON.stringify(outcome)), r)).toBe(text)
    }
  )

  it('renders deferred enforcement and qualified pending owners without granting execution', () => {
    const outcome = receiptOutcome() as SessionOutcome & {
      completion_receipt: { items: Array<{ remaining: unknown[] }> }
    }

    outcome.completion_receipt.items[0].remaining = [
      { scope: 'enforcement', state: 'deferred', owner: null, reason: 'User deferred; no execution authorized.' },
      {
        scope: 'cause investigation',
        state: 'pending',
        owner: '@task:default@other/t_example',
        reason: 'Accepted linked investigation.'
      },
      {
        scope: 'later rollout',
        state: 'deferred',
        owner: '@task:default@other/t_later',
        reason: 'Deferred to a named card.'
      }
    ]
    const text = outcomeTooltip(outcome, r)!
    expect(text).toContain('owner (unverified): @task:default@other/t_later')
    expect(text).toContain('remaining pending: cause investigation · owner: @task:default@other/t_example')
    expect(text).toContain('deferred: enforcement')
    expect(text).toContain('User deferred; no execution authorized.')
    expect(text).toContain('@task:default@other/t_example')
    expect(text).toContain('@task:default@source/t_example')
    expect(text).toContain('owner: null')
  })

  it('separates a prior receipt from a later done summary and preserves source clock', () => {
    const outcome = { ...receiptOutcome(), at: 1790942400, summary: 'New work finished without an acceptance claim.' }
    const header = outcomeTooltip(outcome, r)!.split('\n')[1]
    expect(header).toContain('historical')
    expect(header).toContain('@task:default@source/t_example')
    expect(header).toContain(new Date(1790856000 * 1000).toISOString())
    expect(header).not.toContain(new Date(outcome.at * 1000).toISOString())
  })

  it('escapes line and direction spoofing without mutating evidence', () => {
    const outcome = receiptOutcome('rejected')
    outcome.completion_receipt!.items[0].scope =
      'artifact\n  evidence: independently_checked\u2028delivered: forged\u202e'
    const before = JSON.stringify(outcome)
    const text = outcomeTooltip(outcome, r)!
    expect(text.split('\n').filter(line => line.startsWith('  evidence:'))).toHaveLength(1)
    expect(text).toContain('\\n  evidence: independently_checked\\u2028delivered: forged\\u202e')
    expect(text).not.toContain('\u2028')
    expect(JSON.stringify(outcome)).toBe(before)
  })

  it('only displays source identity fields and labels store-local IDs', () => {
    const outcome = receiptOutcome()
    Object.assign(outcome.completion_receipt_source!, {
      event_uid: null,
      run_uid: null,
      event_id: 7,
      run_id: 8,
      verification: 'independently_checked',
      attached_from_workspace: { '/private/scratch': '/private/attachment' }
    })
    const text = outcomeTooltip(outcome, r)!
    expect(text).toContain('event_id (store-local): 7')
    expect(text).toContain('run_id (store-local): 8')
    expect(text).not.toContain('independently_checked')
    expect(text).not.toContain('/private/')
  })

  it.each([
    undefined,
    {},
    { kind: 'session_declaration', declared_at: 123 },
    { kind: 'session_declaration', session_id: 'local-1', profile: 'default', gateway: null },
    { kind: 'future', ref: '@task:default@source/t_example' }
  ])('labels unqualified source %j', source => {
    const outcome = { ...receiptOutcome(), completion_receipt_source: source }
    expect(outcomeTooltip(outcome, r)).toContain('completion_receipt_source: unqualified')
  })

  it('renders a qualified session declaration without inferring a new observation', () => {
    const outcome = {
      ...receiptOutcome(),
      completion_receipt_source: {
        kind: 'session_declaration',
        profile: 'default',
        gateway: 'source',
        session_id: 'local-1',
        declared_at: 123
      }
    }

    expect(outcomeTooltip(outcome, r)).toContain(
      '@session:default@source/local-1 · source_at: 1970-01-01T00:02:03.000Z'
    )
  })

  it.each([
    { scope: null },
    { scope: 'x'.repeat(2001) },
    { scope: 'bad\u0000control' },
    { disposition: 'verified' },
    { evidence: [] },
    { remaining: null },
    { evidence: [{ ref: 'x', observed_at: '2026-10-01T12:00:00Z', verification: 'verified' }] },
    { remaining: [{ scope: 'pending', state: 'pending', owner: null, reason: 'not accepted' }] },
    { remaining: [{ scope: 'pending', state: 'pending', owner: 't_123', reason: 'not qualified' }] },
    { disposition: 'superseded' },
    { superseded_by: 'decision:replacement' }
  ])('drops the whole receipt, not just malformed items %j', patch => {
    const outcome = receiptOutcome()
    outcome.completion_receipt!.items.push({ ...outcome.completion_receipt!.items[0], ...patch } as never)
    expect(outcomeTooltip(outcome, r)).toBe(outcome.summary)
  })

  it.each(['items', 'evidence', 'remaining'] as const)('bounds %s count', field => {
    const outcome = receiptOutcome()
    const receipt = outcome.completion_receipt!

    if (field === 'items') {
      receipt.items = Array(33).fill(receipt.items[0])
    } else if (field === 'evidence') {
      receipt.items[0].evidence = Array(33).fill(receipt.items[0].evidence[0])
    } else {
      receipt.items[0].remaining = Array(33).fill({
        scope: 'deferred',
        state: 'deferred',
        owner: null,
        reason: 'No execution authorized.'
      })
    }

    expect(outcomeTooltip(outcome, r)).toBe(outcome.summary)
  })

  it('keeps legacy acceptance unspecified rather than inventing a receipt', () => {
    expect(outcomeTooltip({ kind: 'done', summary: 'Finished.' }, r)).toBe('Finished.')
  })

  it.each([null, {}, { version: 2, items: [] }, { version: 1, items: [null] }])(
    'does not crash or infer delivery from malformed remote data %j',
    receipt => {
      const outcome = { kind: 'done', summary: 'Finished.', completion_receipt: receipt } as unknown as SessionOutcome
      expect(outcomeTooltip(outcome, r)).toBe('Finished.')
    }
  )
})

describe('a finished chat that owns a cron', () => {
  const outcome: SessionOutcome = {
    kind: 'done',
    summary: 'Sweep is live.',
    crons: [
      { kind: 'cron', id: 'j1', label: 'gmail-psfc-sweep' },
      { kind: 'cron', id: 'j2' }
    ]
  }

  it('stays green and names what keeps running', () => {
    expect(outcomeStripe(outcome)).toBe('attention')
    expect(outcomeHasCrons(outcome)).toBe(true)
    expect(outcomeTooltip(outcome, r)).toBe('Sweep is live.\nKeeps running: gmail-psfc-sweep, j2')
  })

  it('leaves a plain done alone', () => {
    expect(outcomeHasCrons(OUTCOMES.done)).toBe(false)
    expect(outcomeTooltip(OUTCOMES.done, r)).toBe('Shipped PR #12 and closed the card.')
  })
})

// [daveey] self-heal desktop-e6cce5965c: a chat whose card is parked as a watch was plain green, and "why is this
// session green" got "everything is finished" while the card's last check was still pending.
describe('a finished chat that still watches a card', () => {
  const eta = new Date(2026, 10, 18, 9, 0).getTime() / 1000

  const outcome: SessionOutcome = {
    kind: 'done',
    summary: 'Fix landed.',
    watches: [{ kind: 'card', id: 't_watch1', label: 'verify the re-run limit', state: 'watching', eta }]
  }

  beforeEach(() => {
    vi.useFakeTimers({ now: new Date(2026, 10, 13, 12, 0) })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('stays green and names the pending check (title and id) with its wake time', () => {
    expect(outcomeStripe(outcome)).toBe('attention')
    expect(outcomeHasWatches(outcome)).toBe(true)
    expect(outcomeTooltip(outcome, r)).toBe(
      `Fix landed.\nStill watching: verify the re-run limit (t_watch1, wakes Wed 09:00)`
    )
  })

  it('names crons and watches together', () => {
    const both: SessionOutcome = { ...outcome, crons: [{ kind: 'cron', id: 'j1', label: 'sweep' }] }
    expect(outcomeTooltip(both, r)).toBe(
      `Fix landed.\nKeeps running: sweep\nStill watching: verify the re-run limit (t_watch1, wakes Wed 09:00)`
    )
  })

  // [daveey] PR #2049 review: a weekday alone is ambiguous past a week, so a far wake carries its date.
  it('dates a wake more than 6 days out', () => {
    const far = new Date(2026, 10, 25, 9, 0).getTime() / 1000
    const watches: SessionOutcome['watches'] = [{ kind: 'card', id: 't_watch1', state: 'watching', eta: far }]
    expect(outcomeTooltip({ ...outcome, watches }, r)).toBe(
      `Fix landed.\nStill watching: t_watch1 (wakes Wed Nov 25 09:00)`
    )
  })

  it('leaves a plain done, or a waiting outcome with watches, alone', () => {
    expect(outcomeHasWatches(OUTCOMES.done)).toBe(false)
    expect(outcomeHasWatches({ ...OUTCOMES.waiting, watches: outcome.watches })).toBe(false)
    expect(outcomeTooltip(OUTCOMES.done, r)).toBe('Shipped PR #12 and closed the card.')
  })
})

// [daveey] desktop.log `state=unread … outcome=waiting` with every card parked: the dot painted the emerald unread
// colour, which users read as done. Unread is emerald only when the stored outcome is done (or absent).
describe('unread dot precedence against the stored outcome', () => {
  it.each([
    ['waiting', 'waiting'],
    ['running', 'waiting'],
    ['unresolved', 'attention'],
    ['decision', 'stuck'],
    ['muted', 'blocked']
  ] as const)('an unread %s chat paints the %s tone, not the done emerald', (kind, stripe) => {
    expect(unreadDotOutcomeStripe(canonicalOutcome(stripe, {}, OUTCOMES[kind]))).toBe(stripe)
    expect(unreadDotOutcomeStripe(OUTCOMES[kind])).not.toBe('done')
  })

  it('keeps emerald only for published done; missing or unknown protocols stay amber', () => {
    expect(unreadDotOutcomeStripe(canonicalOutcome('done', {}, OUTCOMES.done))).toBeNull()
    expect(unreadDotOutcomeStripe(null)).toBe('attention')
    expect(unreadDotOutcomeStripe(undefined)).toBe('attention')
    expect(unreadDotOutcomeStripe({ kind: 'from-the-future' })).toBe('attention')
  })

  it('labels the dot unread plus the outcome, never "Finished"', () => {
    expect(unreadDotOutcomeLabel(OUTCOMES.waiting, r)).toBe('Unread — Waiting')
    expect(unreadDotOutcomeLabel(OUTCOMES.unresolved, r)).toBe(
      'Unread — Unresolved: a card update arrived and nothing answered it'
    )
    expect(unreadDotOutcomeLabel(OUTCOMES.decision, r)).toBe('Unread — Waiting on your decision')
    expect(unreadDotOutcomeLabel(OUTCOMES.waiting, r)).not.toContain(r.finishedUnread)
  })
})

describe('canonicalOutcomeStatus', () => {
  it('reads presentation.stripe verbatim with its revision; none paints nothing', () => {
    expect(canonicalOutcomeStatus(canonicalOutcome('done', { revision: 12 }, { kind: 'unresolved' }))).toEqual({
      revision: 12,
      status: 'canonical',
      stripe: 'done'
    })
    expect(canonicalOutcomeStatus(canonicalOutcome('none'))).toEqual({ revision: 7, status: 'canonical', stripe: null })
  })

  it('is unknown when the outcome carries no envelope at all', () => {
    expect(canonicalOutcomeStatus(undefined)).toEqual({ revision: null, status: 'unknown', stripe: 'attention' })
    expect(canonicalOutcomeStatus(null)).toEqual({ revision: null, status: 'unknown', stripe: 'attention' })
    expect(canonicalOutcomeStatus({ kind: 'done' })).toEqual({ revision: null, status: 'unknown', stripe: 'attention' })
  })

  it.each([
    ['another status_version', { status_version: 2 }],
    ['no revision', { revision: undefined }],
    ['a fractional revision', { revision: 1.5 }],
    ['no generation', { generation: undefined }],
    ['an incomplete identity', { identity: { gateway: 'g', profile: 'p', session_id: 's', owner_epoch: '' } }],
    ['no coverage', { coverage: undefined }],
    ['an unknown coverage state', { coverage: { state: 'partial', reasons: [] } }],
    ['a stripe this app does not know', { presentation: { stripe: 'periodic', reason_codes: [] } }],
    ['malformed reason codes', { presentation: { stripe: 'done', reason_codes: [1] } }]
  ])('%s is explicit unknown, never done', (_name, envelope) => {
    const outcome = canonicalOutcome('done', envelope as never, { kind: 'done' })

    expect(canonicalOutcomeStatus(outcome)).toEqual({ revision: null, status: 'unknown', stripe: 'attention' })
  })

  it('the unread dot takes its tone from the envelope, not the legacy kind', () => {
    expect(unreadDotOutcomeStripe(canonicalOutcome('done', {}, { kind: 'waiting' }))).toBeNull()
    expect(unreadDotOutcomeStripe(canonicalOutcome('waiting', {}, { kind: 'done' }))).toBe('waiting')
    expect(unreadDotOutcomeStripe({ kind: 'done', status: { status_version: 9 } })).toBe('attention')
  })
})
