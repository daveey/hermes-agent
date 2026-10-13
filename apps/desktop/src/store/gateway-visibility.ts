import { computed } from 'nanostores'

import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'
import type { SessionInfo } from '@/hermes'
import { Codecs, persistentAtom } from '@/lib/persisted'

import { $connectionsRegistry } from './connection-registry-state'

// Per-gateway sidebar presentation [daveey]: every connected gateway's sessions
// share one sidebar, each painted in its gateway's color, and any gateway can
// be hidden. Both choices are view state only — hiding a gateway never
// disconnects it.

const PREFIX = 'hermes.desktop.sidebar.gateways.v1'

/** Registry connection ids whose sessions the sidebar hides. */
export const $hiddenGatewayIds = persistentAtom<string[]>(`${PREFIX}.hidden`, [], Codecs.stringArray)

/** User-picked color per registry connection id (a palette key or `none`). */
export const $gatewayColorOverrides = persistentAtom<Record<string, string>>(
  `${PREFIX}.colors`,
  {},
  Codecs.stringRecord
)

/** Picker palette. Values are CSS colors that read on both light and dark chrome. */
export const GATEWAY_PALETTE: ReadonlyArray<{ id: string; color: string }> = [
  { id: 'violet', color: '#a78bfa' },
  { id: 'amber', color: '#f59e0b' },
  { id: 'teal', color: '#2dd4bf' },
  { id: 'rose', color: '#fb7185' },
  { id: 'sky', color: '#38bdf8' },
  { id: 'lime', color: '#a3e635' },
  { id: 'orange', color: '#fb923c' },
  { id: 'fuchsia', color: '#e879f9' }
]

export const GATEWAY_COLOR_NONE = 'none'

const paletteColor = (id: string | undefined): null | string =>
  GATEWAY_PALETTE.find(entry => entry.id === id)?.color ?? null

/** The registry connection that owns a row. Bare rows belong to the window
 *  primary (the legacy local door), never "unknown". */
export function sessionGatewayId(
  session: Pick<SessionInfo, 'connection_id'>,
  registry: DesktopConnectionsRegistry | null
): string {
  return session.connection_id?.trim() || registry?.primary || 'local'
}

/**
 * Default palette slot per gateway: the local device stays neutral; every
 * other connection takes the next palette entry in registry order, so colors
 * are stable across launches and a new gateway never repaints the old ones.
 */
export function defaultGatewayColors(
  connections: ReadonlyArray<Pick<DesktopRegistryConnection, 'id' | 'kind'>>
): Record<string, null | string> {
  const out: Record<string, null | string> = {}
  let slot = 0

  for (const connection of connections) {
    if (connection.kind === 'local') {
      out[connection.id] = null

      continue
    }

    out[connection.id] = GATEWAY_PALETTE[slot % GATEWAY_PALETTE.length].id
    slot += 1
  }

  return out
}

/** Palette id per gateway after user overrides (`null` = no color). */
export function resolveGatewayColorIds(
  registry: DesktopConnectionsRegistry | null,
  overrides: Record<string, string>
): Record<string, null | string> {
  const defaults = defaultGatewayColors(registry?.connections ?? [])

  for (const [id, pick] of Object.entries(overrides)) {
    defaults[id] = pick === GATEWAY_COLOR_NONE ? null : paletteColor(pick) ? pick : (defaults[id] ?? null)
  }

  return defaults
}

/** CSS color per gateway id; absent / null = paint nothing. */
export const $gatewayColors = computed([$connectionsRegistry, $gatewayColorOverrides], (registry, overrides) => {
  const ids = resolveGatewayColorIds(registry, overrides)
  const out: Record<string, null | string> = {}

  for (const [id, pick] of Object.entries(ids)) {
    out[id] = paletteColor(pick ?? undefined)
  }

  return out
})

/** Only paint gateway colors when there is more than one gateway to tell apart. */
export const $gatewayColorsActive = computed($connectionsRegistry, registry => (registry?.connections.length ?? 0) > 1)

export function setGatewayColor(connectionId: string, pick: string): void {
  $gatewayColorOverrides.set({ ...$gatewayColorOverrides.get(), [connectionId]: pick })
}

export function toggleGatewayHidden(connectionId: string): void {
  const hidden = $hiddenGatewayIds.get()

  $hiddenGatewayIds.set(
    hidden.includes(connectionId) ? hidden.filter(id => id !== connectionId) : [...hidden, connectionId]
  )
}

export function isGatewayHidden(connectionId: string): boolean {
  return $hiddenGatewayIds.get().includes(connectionId)
}

export function sessionOnHiddenGateway(
  session: Pick<SessionInfo, 'connection_id'>,
  hidden: readonly string[],
  registry: DesktopConnectionsRegistry | null
): boolean {
  return hidden.length > 0 && hidden.includes(sessionGatewayId(session, registry))
}
