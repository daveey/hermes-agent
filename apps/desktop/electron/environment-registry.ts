import { connectionIdForLabel, type ConnectionRegistry } from './connection-registry'

/** The env route is a process-local source, never a saved gateway or credential. */
export class EnvironmentRegistry {
  private views = new WeakMap<ConnectionRegistry, ConnectionRegistry>()
  private environmentId: string | null = null
  private previous: ConnectionRegistry | null = null

  constructor(private readonly env: { url?: string; token?: string }) {}

  read(stored: ConnectionRegistry): ConnectionRegistry {
    // Retain the read baseline: reading the shared store again before its write
    // refreshes its revision and defeats its concurrent-writer rebase.
    this.previous = stored
    const url = String(this.env.url || '').trim()

    if (!url) {
      return stored
    }

    const cached = this.views.get(stored)

    if (cached) {
      return cached
    }

    const id = connectionIdForLabel('Environment gateway', stored.connections.map(c => c.id))

    const view: ConnectionRegistry = {
      ...stored,
      primary: id,
      lastUsed: id,
      connections: [
        ...stored.connections,
        {
          id,
          kind: 'remote',
          label: 'Environment gateway',
          url,
          authMode: 'token',
          token: { encoding: 'plain', value: String(this.env.token || '').trim() }
        }
      ]
    }

    this.environmentId = id
    this.views.set(stored, view)

    return view
  }

  /** Keep edits to saved sources, but never persist the env source or its selection. */
  stored(next: ConnectionRegistry, previous: ConnectionRegistry = this.previous || next): ConnectionRegistry {
    const id = this.environmentId

    if (!id) {
      return next
    }

    return {
      ...next,
      connections: next.connections.filter(c => c.id !== id),
      primary: next.primary === id ? previous.primary : next.primary,
      lastUsed: next.lastUsed === id ? previous.lastUsed : next.lastUsed
    }
  }

  assertEditable(id: unknown): void {
    if (this.environmentId && id === this.environmentId) {
      throw new Error('This gateway is configured by HERMES_DESKTOP_REMOTE_URL; change the environment to edit it.')
    }
  }
}
