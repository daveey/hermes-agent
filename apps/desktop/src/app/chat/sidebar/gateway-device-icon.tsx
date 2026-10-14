import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'
import { Cloud, Laptop, Monitor } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $connectionsRegistry } from '@/store/connection-registry-state'

// [daveey] Which machine a session row lives on, as a small glyph after its
// age: laptop, Mac mini or cloud. The kind is guessed from the connection's
// own name (label / ssh host / id) — the local device has no useful label
// ("This device"), so it uses the backend's hostname, fetched once.

export type GatewayDevice = 'cloud' | 'desktop' | 'laptop' | 'mini'

const LAPTOP_RE = /macbook|laptop|\bmbp\b|book/i
const MINI_RE = /mac-?mini|\bmini\b|-mini|mini-/i

export function gatewayDevice(
  connection: Pick<DesktopRegistryConnection, 'id' | 'kind' | 'label'> & { host?: string },
  localHostname: null | string
): GatewayDevice {
  if (connection.kind === 'cloud') {
    return 'cloud'
  }

  const name =
    connection.kind === 'local'
      ? (localHostname ?? '')
      : [connection.label, connection.host, connection.id].filter(Boolean).join(' ')

  if (MINI_RE.test(name)) {
    return 'mini'
  }

  if (LAPTOP_RE.test(name)) {
    return 'laptop'
  }

  return 'desktop'
}

export const $localHostname = atom<null | string>(null)

let hostnameRequested = false

function requestLocalHostname(registry: DesktopConnectionsRegistry | null) {
  const local = registry?.connections.find(c => c.kind === 'local')

  if (hostnameRequested || !local || typeof window === 'undefined' || !window.hermesDesktop?.api) {
    return
  }

  hostnameRequested = true
  window.hermesDesktop
    .api<{ hostname?: string }>({ connectionId: local.id, path: '/api/system/stats' })
    .then(stats => $localHostname.set(stats?.hostname ?? ''))
    .catch(() => {
      hostnameRequested = false
    })
}

// Mac mini: a low, wide box with its front status light. Same 24px stroke
// grid as the tabler icons it sits beside.
function MacMini({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={2}
      viewBox="0 0 24 24"
    >
      <rect height="8" rx="2.5" width="18" x="3" y="8" />
      <path d="M7 19h10" />
      <circle cx="17" cy="12" fill="currentColor" r="0.5" />
    </svg>
  )
}

const LABEL: Record<GatewayDevice, string> = {
  cloud: 'Cloud',
  desktop: 'Desktop',
  laptop: 'Laptop',
  mini: 'Mac mini'
}

export function GatewayDeviceIcon({ className, gatewayId }: { className?: string; gatewayId: string }) {
  const registry = useStore($connectionsRegistry)
  const localHostname = useStore($localHostname)

  // One gateway: nothing to tell apart.
  if (!registry || registry.connections.length < 2) {
    return null
  }

  const connection = registry.connections.find(c => c.id === gatewayId)

  if (!connection) {
    return null
  }

  if (connection.kind === 'local' && localHostname === null) {
    requestLocalHostname(registry)
  }

  const device = gatewayDevice(connection, localHostname)
  const Icon = device === 'cloud' ? Cloud : device === 'laptop' ? Laptop : device === 'mini' ? MacMini : Monitor

  return (
    <span
      aria-label={`${connection.label} (${LABEL[device]})`}
      className={cn('inline-flex shrink-0 items-center text-(--ui-text-quaternary)', className)}
      data-gateway-device={device}
      data-slot="session-gateway-device"
      role="img"
      title={connection.label}
    >
      <Icon className="size-3" />
    </span>
  )
}
