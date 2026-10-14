import { afterEach, describe, expect, it, vi } from 'vitest'

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
  function setup() {
    const anchor = document.createElement('span')
    document.body.append(anchor)
    publishSidebarSection(key, {
      anchor,
      ids: ['a', 'b', 'c', 'd', 'e'],
      turnIds: ['a', 'c', 'd', 'e'],
      virtualized: true
    })
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'needs-input', e: 'unread' }
  }

  it('lands only on rows with a status stripe, including read-but-finished (green) ones', () => {
    setup()
    expect(nextSidebarSessionId('e', 1, false)).toBe('a') // wraps; idle + turns = green
    expect(nextSidebarSessionId('a', 1, false)).toBe('d') // skips b (no turns) and c (working)
    expect(nextSidebarSessionId('d', -1, false)).toBe('a')
    expect(nextSidebarSessionId('a', -1, false)).toBe('e')
  })

  it('does not fall back to an unstriped neighbour when nothing else has a stripe', () => {
    setup()
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'working', e: 'working' }
    expect(nextSidebarSessionId('a', 1, false)).toBeNull()
    expect(nextSidebarSessionId('a', -1, false)).toBeNull()
  })

  it('archive fallback and ⇧ still take the plain neighbour', () => {
    setup()
    dots.value = { a: 'idle', b: 'idle', c: 'working', d: 'working', e: 'working' }
    expect(nextSidebarSessionId('a', 1, false, true)).toBe('b')
    expect(nextSidebarSessionId('a', 1, true)).toBe('b')
  })

  it('skips sessions marked blocked (⌘B), and finds them again once unblocked', () => {
    setup()
    expect(toggleSessionBlocked('d')).toBe(true)
    expect(nextSidebarSessionId('a', 1, false)).toBe('e') // d is blocked (grey)
    expect(nextSidebarSessionId('e', -1, false)).toBe('a')
    expect(nextSidebarSessionId('a', 1, true)).toBe('b') // ⇧ still steps to anything
    expect(toggleSessionBlocked('d')).toBe(false)
    expect(nextSidebarSessionId('a', 1, false)).toBe('d')
  })
})
