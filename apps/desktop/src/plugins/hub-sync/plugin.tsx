/**
 * [daveey] hub-sync — the Desktop half of the hermes-mods `hub-sync` backend
 * plugin (REST over this gateway's hermes-hub replica). Contributes the
 * left-pane "Synced from other gateways" section: sessions of gateways that
 * are not reachable or not registered right now, with archive / pin / rename
 * sent as hub owner requests. Renders nothing when hub-sync or hermes-hub is
 * absent, so it is on by default.
 */
import { type HermesPlugin, SIDEBAR_SECTIONS_AREA } from '@hermes/plugin-sdk'

import { $hubSessionsOpen, HubSessionsSection } from './sessions-section'

const plugin: HermesPlugin = {
  id: 'hub-sync',
  name: 'Hub Sync',
  description: "Sessions of gateways that are offline right now, from this gateway's hermes-hub replica.",
  defaultEnabled: true,
  register(ctx) {
    $hubSessionsOpen.set(ctx.storage.get('sessionsOpen', true))
    ctx.onDispose($hubSessionsOpen.listen(value => ctx.storage.set('sessionsOpen', value)))

    ctx.registerMany([
      {
        id: 'sessions',
        area: SIDEBAR_SECTIONS_AREA,
        order: 20,
        render: () => <HubSessionsSection />
      }
    ])
  }
}

export default plugin
