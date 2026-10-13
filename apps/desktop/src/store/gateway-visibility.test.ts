import { beforeEach, describe, expect, it } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'

import { $connectionsRegistry } from './connection-registry-state'
import {
  $gatewayColorOverrides,
  $gatewayColors,
  $hiddenGatewayIds,
  defaultGatewayColors,
  GATEWAY_COLOR_NONE,
  GATEWAY_PALETTE,
  sessionGatewayId,
  sessionOnHiddenGateway,
  setGatewayColor,
  toggleGatewayHidden
} from './gateway-visibility'

const registry = {
  version: 2,
  primary: 'local',
  secureTokenStorage: true,
  connections: [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'nous-1', kind: 'cloud', label: 'nous-1' },
    { id: 'mac-mini', kind: 'ssh', label: 'mac-mini' }
  ]
} as unknown as DesktopConnectionsRegistry

describe('gateway visibility', () => {
  beforeEach(() => {
    $hiddenGatewayIds.set([])
    $gatewayColorOverrides.set({})
    $connectionsRegistry.set(registry)
  })

  it('local stays neutral; remotes take palette slots in registry order', () => {
    expect(defaultGatewayColors(registry.connections)).toEqual({
      local: null,
      'nous-1': GATEWAY_PALETTE[0].id,
      'mac-mini': GATEWAY_PALETTE[1].id
    })
    expect($gatewayColors.get()).toEqual({
      local: null,
      'nous-1': GATEWAY_PALETTE[0].color,
      'mac-mini': GATEWAY_PALETTE[1].color
    })
  })

  it('user picks override defaults, including turning color off', () => {
    setGatewayColor('local', 'teal')
    setGatewayColor('mac-mini', GATEWAY_COLOR_NONE)

    expect($gatewayColors.get().local).toBe(GATEWAY_PALETTE.find(p => p.id === 'teal')!.color)
    expect($gatewayColors.get()['mac-mini']).toBeNull()
  })

  it('bare rows belong to the registry primary', () => {
    expect(sessionGatewayId({}, registry)).toBe('local')
    expect(sessionGatewayId({ connection_id: 'mac-mini' }, registry)).toBe('mac-mini')
  })

  it('toggling hides exactly that gateway', () => {
    toggleGatewayHidden('mac-mini')
    const hidden = $hiddenGatewayIds.get()

    expect(sessionOnHiddenGateway({ connection_id: 'mac-mini' }, hidden, registry)).toBe(true)
    expect(sessionOnHiddenGateway({ connection_id: 'nous-1' }, hidden, registry)).toBe(false)
    expect(sessionOnHiddenGateway({}, hidden, registry)).toBe(false)

    toggleGatewayHidden('mac-mini')
    expect($hiddenGatewayIds.get()).toEqual([])
  })
})
