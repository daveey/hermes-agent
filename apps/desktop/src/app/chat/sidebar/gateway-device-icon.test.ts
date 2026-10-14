import { describe, expect, it } from 'vitest'

import { gatewayDevice } from './gateway-device-icon'

describe('gatewayDevice', () => {
  it('maps cloud connections to the cloud glyph', () => {
    expect(gatewayDevice({ id: 'x', kind: 'cloud', label: 'nous-1' }, null)).toBe('cloud')
  })

  it('reads the local device from its hostname', () => {
    const local = { id: 'local', kind: 'local', label: 'This device' } as const

    expect(gatewayDevice(local, 'daveey-mini.local')).toBe('mini')
    expect(gatewayDevice(local, 'MacBook-Pro-177')).toBe('laptop')
    expect(gatewayDevice(local, null)).toBe('desktop')
  })

  it('reads remote devices from label, host, or id', () => {
    expect(gatewayDevice({ host: 'test-key-gen', id: 'macbook', kind: 'ssh', label: 'macbook' }, null)).toBe('laptop')
    expect(gatewayDevice({ host: 'davids-mac-mini-1', id: 'm', kind: 'ssh', label: 'home' }, null)).toBe('mini')
    expect(gatewayDevice({ id: 'box', kind: 'ssh', label: 'workstation' }, null)).toBe('desktop')
  })
})
