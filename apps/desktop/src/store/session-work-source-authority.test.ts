import { afterEach, describe, expect, it } from 'vitest'

import * as store from './session-work-source'

const scope = { connectionId: 'gateway', profile: 'default', storedSessionId: 'chat' }
const sources = ['lineage', 'process', 'subagent', 'tasks', 'control'] as const
const context = { durable: 10, runtime: 20 }
const epoch = (source: string) => (source === 'lineage' || source === 'tasks' ? context.durable : context.runtime)
const complete = () => store.workSourceAuthorityComplete(scope.connectionId, scope.profile, ['chat'], context)

afterEach(() => store.$unavailableWorkSources.set({}))

describe('captured positive read authority', () => {
  it('requires every current source, fails pending and failed reads closed, and permits same-source recovery', () => {
    expect(complete()).toBe(false)

    for (const source of sources) {
      expect(complete()).toBe(false)
      store.reportWorkSourceRead({ ...scope, source, available: true, authorityEpoch: epoch(source) })
    }

    expect(complete()).toBe(true)
    store.reportWorkSourceRead({ ...scope, source: 'control', available: false, authorityEpoch: 20 })
    expect(complete()).toBe(false)
    store.reportWorkSourceRead({ ...scope, source: 'tasks', available: true, authorityEpoch: 10 })
    expect(complete()).toBe(false)
    store.reportWorkSourceRead({ ...scope, source: 'control', available: true, authorityEpoch: 20 })
    expect(complete()).toBe(true)
  })

  it('does not certify tokenless success, wrong scopes, or obsolete runtime authority', () => {
    for (const source of sources) {
      store.reportWorkSourceRead({ ...scope, source, available: true })
    }
    expect(complete()).toBe(false)

    for (const source of sources) {
      store.reportWorkSourceRead({ ...scope, source, available: true, authorityEpoch: epoch(source) })
    }

    expect(complete()).toBe(true)
    expect(store.workSourceAuthorityComplete('other', 'default', ['chat'], context)).toBe(false)
    expect(store.workSourceAuthorityComplete('gateway', 'other', ['chat'], context)).toBe(false)
    expect(store.workSourceAuthorityComplete('gateway', 'default', ['other'], context)).toBe(false)
    expect(store.workSourceAuthorityComplete('gateway', 'default', ['chat'], { durable: 10, runtime: 21 })).toBe(false)
    expect(store.workSourceAuthorityComplete('gateway', 'default', [], context)).toBe(false)
    expect(store.workSourceAuthorityComplete(undefined, 'default', ['chat'], context)).toBe(false)
  })

  it('fences overlapping reads in the same authority epoch', () => {
    const first = store.beginWorkSourceRead({ ...scope, source: 'process', authorityEpoch: 20 })
    const second = store.beginWorkSourceRead({ ...scope, source: 'process', authorityEpoch: 20 })
    store.reportWorkSourceRead({ ...first, available: true })
    expect(store.workSourceUnavailable('gateway', 'default', ['chat'])).toBe(true)
    store.reportWorkSourceRead({ ...second, available: true })
    expect(store.workSourceUnavailable('gateway', 'default', ['chat'])).toBe(false)
    store.reportWorkSourceRead({ ...first, available: false })
    expect(store.workSourceUnavailable('gateway', 'default', ['chat'])).toBe(false)
  })

  it('rejects an old reply after newer pending evidence without destroying newer success', () => {
    for (const source of sources) {
      store.reportWorkSourceRead({ ...scope, source, available: true, authorityEpoch: epoch(source) })
    }

    store.reportWorkSourceRead({ ...scope, source: 'process', available: false, authorityEpoch: 21 })
    store.reportWorkSourceRead({ ...scope, source: 'process', available: true, authorityEpoch: 20 })
    expect(store.workSourceUnavailable('gateway', 'default', ['chat'])).toBe(true)
    store.reportWorkSourceRead({ ...scope, source: 'process', available: true, authorityEpoch: 21 })
    store.reportWorkSourceRead({ ...scope, source: 'process', available: false, authorityEpoch: 20 })
    expect(store.workSourceUnavailable('gateway', 'default', ['chat'])).toBe(false)
  })
})
