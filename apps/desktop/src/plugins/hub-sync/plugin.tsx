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

import { loadReplicaSidebarRows, replicaCursor } from './api'

// [daveey] The replica is fresh within about a second (`hermes-hub watch`). Poll its cursor (a cheap local read) and
// reload rows only when it moved, plus a full reload at least every FULL_MS so owner freshness (asleep) re-evaluates.
const CURSOR_POLL_MS = 2_000
const FULL_MS = 30_000

const plugin: HermesPlugin = {
  id: 'hub-sync',
  name: 'Hub Sync',
  description:
    "Keeps sessions of gateways that are offline right now in the sidebar, from this gateway's hermes-hub replica.",
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

        if (!disposed && records) {
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

    let lastCursor: null | number = null
    let lastFull = Date.now()

    const tick = async () => {
      if (disposed) {
        return
      }

      const cursor = await replicaCursor().catch(() => null)

      if (cursor !== lastCursor || Date.now() - lastFull >= FULL_MS) {
        lastCursor = cursor
        lastFull = Date.now()
        void refresh()
      }
    }

    const timer = setInterval(() => void tick(), CURSOR_POLL_MS)
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
