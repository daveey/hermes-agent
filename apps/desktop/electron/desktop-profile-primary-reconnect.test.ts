import { describe, expect, it } from 'vitest'

import { desktopConnectionRequestInheritsPrimary, resolveDesktopConnectionRequest } from './desktop-profile'

const foreground = { connectionId: 'other', profile: 'coding', registryScoped: true }

describe('primary reconnect ownership', () => {
  it('retains a named legacy alias and primary intent instead of inheriting the foreground', () => {
    expect(resolveDesktopConnectionRequest('client-alias', foreground, 'primary', true)).toEqual({
      connectionId: null,
      profile: 'client-alias'
    })
    expect(desktopConnectionRequestInheritsPrimary('client-alias', foreground, true)).toBe(true)
    expect(resolveDesktopConnectionRequest(undefined, foreground, 'primary', true)).toEqual({
      connectionId: null,
      profile: 'primary'
    })
  })

  it('preserves ordinary explicit profile routing and profile-less foreground routing', () => {
    expect(resolveDesktopConnectionRequest('local-profile', foreground, 'primary')).toEqual({
      connectionId: null,
      profile: 'local-profile'
    })
    expect(desktopConnectionRequestInheritsPrimary('local-profile', foreground)).toBe(false)
    expect(resolveDesktopConnectionRequest(undefined, foreground, 'primary')).toEqual({
      connectionId: 'other',
      profile: 'coding'
    })
  })
})
