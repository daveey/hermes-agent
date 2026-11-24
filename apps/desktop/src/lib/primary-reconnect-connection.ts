import type { HermesConnection } from '@/global'

/** Recover the socket owner, never the mutable foreground or a same-name local pool. */
export function primaryReconnectConnection(desktop: Window['hermesDesktop'], connection: HermesConnection | null) {
  if (connection?.registryScoped && connection.connectionId) {
    return (
      desktop.getConnectionFor?.({ connectionId: connection.connectionId, profile: connection.profile }) ??
      Promise.reject(new Error('Registry gateway connection is unavailable'))
    )
  }

  if (connection?.inheritsPrimary) {
    return desktop.getConnection(connection.profile, { inheritsPrimary: true })
  }

  return desktop.getConnection(connection?.profile)
}
