import { afterEach, describe, expect, it, vi } from 'vitest'

import { canonicalOutcome } from '@/test/canonical-outcome'

import type * as DotStateModule from './session-dot-state'
import type { SessionDotState } from './session-dot-state'

const dots = vi.hoisted(() => ({ value: {} as Record<string, SessionDotState> }))

vi.mock('@/store/session-dot-state', async importOriginal => {
  const real = await importOriginal<typeof DotStateModule>()
  const { atom: mkAtom } = await import('nanostores')
  const $dots = mkAtom<Record<string, SessionDotState>>({})
  $dots.get = () => dots.value

  return { ...real, $sessionDotStateById: $dots }
})

import { $sidebarShowAutomated } from './layout'
import { $sessions } from './session'
import { $blockedSessionIds, toggleSessionBlocked } from './session-blocked'
import {
  nextSidebarSessionId,
  publishSidebarSection,
  sidebarSessionOrder,
  stepSessionId,
  unpublishSidebarSection
} from './sidebar-visible-order'

const order = ['a', 'b', 'c', 'd']

describe('stepSessionId', () => {
  it('steps forward and back with wrap-around', () => {
    expect(stepSessionId(order, 'b', 1)).toBe('c')
    expect(stepSessionId(order, 'd', 1)).toBe('a')
    expect(stepSessionId(order, 'a', -1)).toBe('d')
  })

  it('skips ineligible sessions and wraps to find one', () => {
    const needs = new Set(['a', 'c'])

    expect(stepSessionId(order, 'c', 1, id => needs.has(id))).toBe('a')
    expect(stepSessionId(order, 'a', -1, id => needs.has(id))).toBe('c')
  })

  it('returns null when nothing else qualifies', () => {
    expect(stepSessionId(order, 'b', 1, id => id === 'b')).toBeNull()
    expect(stepSessionId([], 'b', 1)).toBeNull()
  })

  it('starts at the ends for an unknown current session', () => {
    expect(stepSessionId(order, null, 1)).toBe('a')
    expect(stepSessionId(order, 'zz', -1)).toBe('d')
  })
})

describe('sidebarSessionOrder', () => {
  const keys: symbol[] = []

  afterEach(() => {
    keys.forEach(unpublishSidebarSection)
    keys.length = 0
    document.body.innerHTML = ''
  })

  it('concatenates sections in DOM order, deduped', () => {
    const top = document.createElement('span')
    const bottom = document.createElement('span')
    document.body.append(top, bottom)
    const pinned = Symbol('pinned')
    const recents = Symbol('recents')
    keys.push(pinned, recents)

    // Published out of order on purpose.
    publishSidebarSection(recents, { anchor: bottom, ids: ['r1', 'p1', 'r2'] })
    publishSidebarSection(pinned, { anchor: top, ids: ['p1', 'p2'] })

    expect(sidebarSessionOrder()).toEqual(['p1', 'p2', 'r1', 'r2'])
  })

  it('reads drawn row order from the DOM, not the flat row model', () => {
    // Projects / grouped views draw rows in a different order than `ids`.
    const group = document.createElement('div')
    const anchor = document.createElement('span')
    group.append(anchor)

    for (const id of ['g2', 'g1', 'g3']) {
      const row = document.createElement('div')
      row.dataset.sidebarSessionId = id
      group.append(row)
    }

    document.body.append(group)
    const recents = Symbol('recents')
    keys.push(recents)
    publishSidebarSection(recents, { anchor, ids: ['g1', 'g2', 'g3'] })

    expect(sidebarSessionOrder()).toEqual(['g2', 'g1', 'g3'])
  })

  it('trusts the row model for the virtualized list (off-screen rows unmounted)', () => {
    const group = document.createElement('div')
    const anchor = document.createElement('span')
    const row = document.createElement('div')
    row.dataset.sidebarSessionId = 'v2'
    group.append(anchor, row)
    document.body.append(group)
    const recents = Symbol('recents')
    keys.push(recents)
    publishSidebarSection(recents, { anchor, ids: ['v1', 'v2', 'v3'], virtualized: true })

    expect(sidebarSessionOrder()).toEqual(['v1', 'v2', 'v3'])
  })

  it('ignores sections whose anchor left the document', () => {
    const gone = Symbol('gone')
    keys.push(gone)
    publishSidebarSection(gone, { anchor: document.createElement('span'), ids: ['x'] })

    expect(sidebarSessionOrder()).not.toContain('x')
  })
})

describe('nextSidebarSessionId (⌘] / ⌘[)', () => {
  const key = Symbol('recents')

  afterEach(() => {
    unpublishSidebarSection(key)
    dots.value = {}
    $blockedSessionIds.set([])
    document.body.innerHTML = ''
  })

  // a: green (read, finished turn)  b: no stripe (no turns)  c: working
  // d: red (blocked)  e: green (unread)
  function setup(
    verified = true,
    statuses: Record<string, 'done' | 'none' | 'stuck'> = { a: 'done', b: 'none', c: 'none', d: 'stuck', e: 'done' }
  ) {
    const anchor = document.createElement('span')
    document.body.append(anchor)
    publishSidebarSection(key, {
      anchor,
      rows: verified
        ? ['a', 'b', 'c', 'd', 'e'].map(id => ({ id, outcome: canonicalOutcome(statuses[id]) }) as never)
        : [],
      ids: ['a', 'b', 'c', 'd', 'e'],
      turnIds: ['a', 'c', 'd', 'e'],
      virtualized: true
    })
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'needs-input', e: 'unread' }
  }

  it('does not treat a missing offscreen row as verified done', () => {
    setup(false)
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'working', e: 'idle' }
    expect(nextSidebarSessionId('a', 1, false)).toBeNull()
  })

  it.each([{ kanban_active: 1 }, { kanban_unknown: true }, { kanban_observed_at: 1 }])(
    'offers unverified extant tree rows as unknown attention: %j',
    evidence => {
      setup()
      const anchor = document.createElement('span')
      document.body.append(anchor)
      publishSidebarSection(key, {
        anchor,
        ids: ['a', 'e'],
        turnIds: ['a', 'e'],
        virtualized: true,
        rows: [{ id: 'e', kanban_observed_at: Date.now() / 1000, ...evidence } as never]
      })
      expect(nextSidebarSessionId('a', 1, false)).toBe('e')
    }
  )

  it('lands only on rows with a status stripe, including read-but-finished (green) ones', () => {
    setup()
    expect(nextSidebarSessionId('e', 1, false)).toBe('a') // wraps; idle + turns = green
    expect(nextSidebarSessionId('a', 1, false)).toBe('d') // skips b (no turns) and c (working)
    expect(nextSidebarSessionId('d', -1, false)).toBe('a')
    expect(nextSidebarSessionId('a', -1, false)).toBe('e')
  })

  it('does not fall back to an unstriped neighbour when nothing else has a stripe', () => {
    setup(true, { a: 'done', b: 'none', c: 'none', d: 'none', e: 'none' })
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'working', e: 'working' }
    expect(nextSidebarSessionId('a', 1, false)).toBeNull()
    expect(nextSidebarSessionId('a', -1, false)).toBeNull()
  })

  it('archive fallback and ⇧ still take the plain neighbour', () => {
    setup(true, { a: 'done', b: 'none', c: 'none', d: 'none', e: 'none' })
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'working', e: 'working' }
    expect(nextSidebarSessionId('a', 1, false, true)).toBe('b')
    expect(nextSidebarSessionId('a', 1, true)).toBe('b')
  })

  it('skips sessions marked blocked (⌘B), and finds them again once unblocked', () => {
    setup()
    expect(toggleSessionBlocked('d')).toBe(true)
    expect(nextSidebarSessionId('a', 1, false)).toBe('e') // d is blocked (grey)
    expect(nextSidebarSessionId('e', -1, false)).toBe('a')
    expect(nextSidebarSessionId('c', 1, true)).toBe('e') // ⇧ skips it too
    expect(nextSidebarSessionId('e', -1, true)).toBe('c')
    expect(toggleSessionBlocked('d')).toBe(false)
    expect(nextSidebarSessionId('a', 1, false)).toBe('d')
  })

  it('trusts the stripe a drawn row paints over the section row model (projects view)', () => {
    // The projects overview draws lane rows the section's flat model doesn't
    // carry, so the model has no `message_count` for them: a green row there
    // used to be skipped. The drawn row's own stripe is the authority.
    const group = document.createElement('div')
    const anchor = document.createElement('span')
    group.append(anchor)

    for (const [id, stripe] of [
      ['p1', 'done'],
      ['p2', ''],
      ['p3', 'done'],
      ['p4', 'blocked'],
      ['p5', 'stuck']
    ] as const) {
      const row = document.createElement('div')
      row.dataset.sidebarSessionId = id

      if (stripe) {
        row.dataset.sidebarStripe = stripe
      }

      group.append(row)
    }

    document.body.append(group)
    publishSidebarSection(key, { anchor, ids: ['other'], turnIds: [] })
    dots.value = { p1: 'idle', p2: 'idle', p3: 'idle', p4: 'idle', p5: 'needs-input' }

    expect(nextSidebarSessionId('p1', 1, false)).toBe('p3')
    expect(nextSidebarSessionId('p3', 1, false)).toBe('p5') // p4 is grey (⌘B)
    expect(nextSidebarSessionId('p5', 1, false)).toBe('p1') // wraps
    expect(nextSidebarSessionId('p1', -1, false)).toBe('p5')
  })
})

describe('automated / test rows (sessions.automated_sources) [daveey]', () => {
  const key = Symbol('recents')

  afterEach(() => {
    unpublishSidebarSection(key)
    dots.value = {}
    $sessions.set([])
    $sidebarShowAutomated.set(false)
    document.body.innerHTML = ''
  })

  // a, c: green human rows; b: green automated row (hidden by default).
  function setup() {
    const anchor = document.createElement('span')
    document.body.append(anchor)
    publishSidebarSection(key, { anchor, ids: ['a', 'b', 'c'], turnIds: ['a', 'b', 'c'], virtualized: true })
    dots.value = { a: 'idle', b: 'idle', c: 'idle' }
    $sessions.set([
      { id: 'a', message_count: 2, kanban_observed_at: Date.now() / 1000 },
      { automated: true, id: 'b', message_count: 2, kanban_observed_at: Date.now() / 1000 },
      { id: 'c', message_count: 2, kanban_observed_at: Date.now() / 1000 }
    ] as never)
  }

  it('drops hidden automated rows from the visible order, and stepping skips them', () => {
    setup()
    expect(sidebarSessionOrder()).toEqual(['a', 'c'])
    expect(nextSidebarSessionId('a', 1, false)).toBe('c')
    expect(nextSidebarSessionId('a', 1, true)).toBe('c')
  })

  it('steps onto them again once "Show automated & test sessions" is on', () => {
    setup()
    $sidebarShowAutomated.set(true)
    expect(sidebarSessionOrder()).toEqual(['a', 'b', 'c'])
    expect(nextSidebarSessionId('a', 1, false)).toBe('b')
  })

  it('keeps an automated row that needs the user (red stripe) in the order', () => {
    setup()
    dots.value = { a: 'idle', b: 'needs-input', c: 'idle' }
    expect(sidebarSessionOrder()).toEqual(['a', 'b', 'c'])
    expect(nextSidebarSessionId('a', 1, false)).toBe('b')
  })
})
