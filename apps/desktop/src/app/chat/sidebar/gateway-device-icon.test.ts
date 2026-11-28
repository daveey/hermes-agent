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

  it('shows remote cloud/server gateways as cloud, not a desktop', () => {
    // The hub: an EC2 box reached over SSH at a bare tailnet IP.
    expect(
      gatewayDevice(
        {
          host: '100.104.201.88',
          id: 'hermes-hub',
          kind: 'ssh',
          label: 'hub (EC2)',
          remoteHermesPath: '/home/ubuntu/.local/bin/hermes',
          user: 'ubuntu'
        },
        null
      )
    ).toBe('cloud')
    expect(gatewayDevice({ host: '10.0.0.5', id: 'g', kind: 'ssh', label: 'gpu', user: 'ubuntu' }, null)).toBe('cloud')
    expect(
      gatewayDevice(
        { host: '10.0.0.6', id: 'v', kind: 'ssh', label: 'box', remoteHermesPath: '/root/.local/bin/hermes' },
        null
      )
    ).toBe('cloud')
    expect(gatewayDevice({ id: 'r', kind: 'remote', label: 'aws-worker' }, null)).toBe('cloud')
    // A Mac reached over SSH stays a Mac.
    expect(
      gatewayDevice(
        {
          host: 'davids-mac-mini-1',
          id: 'mac-mini',
          kind: 'ssh',
          label: 'mac-mini',
          user: 'daveey',
          remoteHermesPath: '/Users/daveey/code/hermes-agent/.venv/bin/hermes'
        },
        null
      )
    ).toBe('mini')
  })
})
