/**
 * [daveey] hub-sync — the Desktop half of the hermes-mods `hub-sync` backend
 * plugin (REST over this gateway's hermes-hub replica). A background service:
 * it keeps the replica's session rows of unreachable gateways in the sidebar's
 * fallback store, and the ORDINARY sidebar list shows them (marked offline)
 * under its normal visibility / automated / profile / gateway / pin / grouping
 * rules. It contributes no sidebar section of its own. Renders nothing and
 * does nothing when hub-sync or hermes-hub is absent, so it is on by default.
 */
import { type HermesPlugin, host, publishOfflineSessions } from '@hermes/plugin-sdk'

import { loadReplicaSidebarRows } from './api'

const POLL_MS = 60_000

const plugin: HermesPlugin = {
  id: 'hub-sync',
  name: 'Hub Sync',
  description: "Keeps sessions of gateways that are offline right now in the sidebar, from this gateway's hermes-hub replica.",
  defaultEnabled: true,
  register(ctx) {
    let disposed = false
    let running = false
    let again = false

    const refresh = async () => {
      if (running) {
        again = true

        return
      }

      running = true

      try {
        const records = await loadReplicaSidebarRows(() => !disposed)

        if (!disposed) {
          publishOfflineSessions(records)
        }
      } catch {
        // Best effort: the live sidebar is untouched when the replica is unreadable.
      } finally {
        running = false

        if (again && !disposed) {
          again = false
          void refresh()
        }
      }
    }

    void refresh()

    const timer = setInterval(() => void refresh(), POLL_MS)
    const stopGateways = host.state.gateways.listen(() => void refresh())

    ctx.onDispose(() => {
      disposed = true
      clearInterval(timer)
      stopGateways()
      publishOfflineSessions([])
    })
  }
}

export default plugin
