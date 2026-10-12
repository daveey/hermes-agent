import { afterEach, describe, expect, it } from 'vitest'

import {
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

  it('ignores sections whose anchor left the document', () => {
    const gone = Symbol('gone')
    keys.push(gone)
    publishSidebarSection(gone, { anchor: document.createElement('span'), ids: ['x'] })

    expect(sidebarSessionOrder()).not.toContain('x')
  })
})
