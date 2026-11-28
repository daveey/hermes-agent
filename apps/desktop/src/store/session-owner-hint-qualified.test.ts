import { afterEach, describe, expect, it } from 'vitest'

import {
  _resetSessionOwnerHintsForTests,
  getSessionOwnerHints,
  hydrateSessionOwnerHints,
  setSessionOwnerHint
} from '@/store/session'

const KEY = 'hermes.desktop.sessionOwnerHints.v1'

describe('session owner hints reject gateway-qualified profiles', () => {
  afterEach(() => {
    _resetSessionOwnerHintsForTests({ storage: true })
  })

  it('never remembers a `profile@gateway` route', () => {
    setSessionOwnerHint('s-bad', { connectionId: 'mac-mini', profile: 'default@mac-mini' })
    setSessionOwnerHint('s-bad2', { connectionId: 'mac-mini', profile: 'default', targetProfile: 'default@mac-mini' })
    setSessionOwnerHint('s-ok', { connectionId: 'mac-mini', profile: 'default' })

    expect(getSessionOwnerHints('s-bad')).toEqual([])
    expect(getSessionOwnerHints('s-bad2')).toEqual([])
    expect(getSessionOwnerHints('s-ok')).toEqual([{ connectionId: 'mac-mini', profile: 'default' }])
  })

  it('drops poisoned persisted hints on hydrate and keeps valid ones', () => {
    _resetSessionOwnerHintsForTests({ storage: true })
    window.localStorage.setItem(
      KEY,
      JSON.stringify([
        ['s-bad', { connectionId: 'mac-mini', profile: 'default@mac-mini', targetProfile: 'default@mac-mini' }],
        ['s-ok', { connectionId: 'mac-mini', profile: 'default' }]
      ])
    )

    hydrateSessionOwnerHints()

    expect(getSessionOwnerHints('s-bad')).toEqual([])
    expect(getSessionOwnerHints('s-ok')).toEqual([{ connectionId: 'mac-mini', profile: 'default' }])
  })
})
