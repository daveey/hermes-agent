import { computed } from 'nanostores'

import { $connectionsRegistry } from '@/store/connection-registry-state'

/** [daveey] Every registered gateway (id, kind, label), registry order. Token
 *  bytes and ssh details never cross into plugins. Empty on legacy
 *  profile-only Desktop builds. */
export interface PluginGateway {
  id: string
  kind: string
  label: string
}

export const $gateways = computed($connectionsRegistry, (registry): PluginGateway[] =>
  (registry?.connections ?? []).map(({ id, kind, label }) => ({ id, kind, label }))
)
