import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from '@/i18n/catalog'
import { type SessionDotState, sessionStatusStripe } from '@/store/session-dot-state'
import type { SessionOutcome } from '@/types/session-outcome'

import { outcomeEtaLabel, outcomeHasCrons, outcomeStripe, outcomeTooltip, outcomeWaitingLine } from './session-outcome'

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

describe('sessionStatusStripe with a stored outcome', () => {
  it.each([
    ['done', 'done'],
    ['decision', 'stuck'],
    ['muted', 'blocked'],
    ['waiting', 'waiting'],
    ['running', 'waiting'],
    ['unresolved', 'attention']
  ] as const)('%s paints %s', (kind, stripe) => {
    expect(outcomeStripe(OUTCOMES[kind])).toBe(stripe)
    // Wins over every derived input that would otherwise paint something else.
    expect(sessionStatusStripe('idle', true, false, 3, true, 'stuck', 2, 4, 1, OUTCOMES[kind])).toBe(stripe)
    expect(sessionStatusStripe('background', false, false, 0, false, null, 0, 0, 0, OUTCOMES[kind])).toBe(stripe)
    expect(sessionStatusStripe('unread', true, false, 0, false, null, 0, 0, 0, OUTCOMES[kind])).toBe(stripe)
  })

  it.each(OLD_LOGIC_CASES)('outcome absent keeps the old answer: %j', (...inputs) => {
    const [state, hasTurns, userBlocked, kBlocked, periodic, review, attention, waiting, stalled] = inputs

    const legacy = sessionStatusStripe(
      state,
      hasTurns,
      userBlocked,
      kBlocked,
      periodic,
      review,
      attention,
      waiting,
      stalled
    )

    expect(
      sessionStatusStripe(
        state,
        hasTurns,
        userBlocked,
        kBlocked,
        periodic,
        review,
        attention,
        waiting,
        stalled,
        undefined
      )
    ).toBe(legacy)
    expect(
      sessionStatusStripe(state, hasTurns, userBlocked, kBlocked, periodic, review, attention, waiting, stalled, null)
    ).toBe(legacy)
    // A kind this app does not know falls back too, rather than painting a guess.
    expect(
      sessionStatusStripe(state, hasTurns, userBlocked, kBlocked, periodic, review, attention, waiting, stalled, {
        kind: 'from-the-future'
      })
    ).toBe(legacy)
  })

  it('pins the old answers the fallback must keep', () => {
    expect(sessionStatusStripe('idle', true)).toBe('done')
    expect(sessionStatusStripe('idle', false)).toBeNull()
    expect(sessionStatusStripe('needs-input', true)).toBe('stuck')
    expect(sessionStatusStripe('idle', true, true)).toBe('blocked')
    expect(sessionStatusStripe('idle', true, false, 0, false, null, 1)).toBe('attention')
    expect(sessionStatusStripe('background', true, false, 0, false, null, 0, 2, 1)).toBe('stalled')
  })

  it.each(['working', 'stalled', 'needs-input'] as SessionDotState[])(
    'the live dot (%s) still overrides every outcome',
    state => {
      for (const outcome of Object.values(OUTCOMES)) {
        expect(sessionStatusStripe(state, true, false, 0, false, null, 0, 0, 0, outcome)).toBe(
          sessionStatusStripe(state, true)
        )
      }
    }
  )

  it('a ⌘B park still beats the outcome', () => {
    for (const outcome of Object.values(OUTCOMES)) {
      expect(sessionStatusStripe('idle', true, true, 0, false, null, 0, 0, 0, outcome)).toBe('blocked')
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

describe('a finished chat that owns a cron', () => {
  const outcome: SessionOutcome = {
    kind: 'done',
    summary: 'Sweep is live.',
    crons: [{ kind: 'cron', id: 'j1', label: 'gmail-psfc-sweep' }, { kind: 'cron', id: 'j2' }]
  }

  it('stays green and names what keeps running', () => {
    expect(outcomeStripe(outcome)).toBe('done')
    expect(outcomeHasCrons(outcome)).toBe(true)
    expect(outcomeTooltip(outcome, r)).toBe('Sweep is live.\nKeeps running: gmail-psfc-sweep, j2')
  })

  it('leaves a plain done alone', () => {
    expect(outcomeHasCrons(OUTCOMES.done)).toBe(false)
    expect(outcomeTooltip(OUTCOMES.done, r)).toBe('Shipped PR #12 and closed the card.')
  })
})
