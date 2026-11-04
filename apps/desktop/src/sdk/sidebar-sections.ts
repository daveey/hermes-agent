// Fork sidebar chrome on the plugin SDK, moved out of ./index.ts unchanged and re-exported there. [daveey]

/** [daveey] Chrome for a `SIDEBAR_SECTIONS_AREA` contribution (left pane,
 *  between Pinned and Projects): the collapsible header + capped body of the
 *  core sections, and a clickable row on the session-row grid. */
export {
  SidebarSection,
  type SidebarSectionProps,
  SidebarSectionRow,
  type SidebarSectionRowProps
} from '@/app/chat/sidebar/contributed-sections'
/** [daveey] The sidebar session row's "which machine" glyph (laptop / Mac
 *  mini / cloud) for a registry connection id. Renders nothing with fewer
 *  than two gateways, so single-gateway users see no change. */
export { GatewayDeviceIcon } from '@/app/chat/sidebar/gateway-device-icon'
