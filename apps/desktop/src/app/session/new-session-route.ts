import { LOCAL_CONNECTION_ID } from '@hermes/shared'

import { NO_PROJECT_ID } from '@/app/chat/sidebar/projects/workspace-groups'
import { translateNow } from '@/i18n/runtime'
import { $connectionsRegistry, thisDeviceLabel } from '@/store/connections'
import { $defaultProfileRoute } from '@/store/default-profile'
import { notify, notifyError } from '@/store/notifications'
import {
  $newChatProfile,
  $newChatRoute,
  type AgentProfileRoute,
  captureNewChatSource,
  ensureGatewayAgent,
  ensureGatewayProfile,
  newSessionInAgent,
  pinLegacyNewChatProfile
} from '@/store/profile'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import {
  isPeerInstanceWindow,
  isProfilePinnedWindow,
  windowConnectionOverride,
  windowProfileOverride
} from '@/store/windows'

/** Only generic New Session actions consult this preference. Explicit profile,
 * agent, project and existing-session actions keep their captured owner. */
export function defaultNewSessionTarget(): { profile: string; route: AgentProfileRoute | null } | null {
  const project = $projectScope.get()

  if (project !== ALL_PROJECTS && project !== NO_PROJECT_ID) {
    return null
  }

  // An ordinary New Window inherits its opener only for boot. Reusing that
  // seed here would undo a later device/profile selection. Only an explicit
  // "Open profile in new window" makes the peer's launch route a default.
  const profile = !isPeerInstanceWindow() || isProfilePinnedWindow() ? windowProfileOverride() : null
  const saved = profile ? { connectionId: windowConnectionOverride(), profile } : $defaultProfileRoute.get()

  if (!saved) {
    return null
  }

  // A captured target can explicitly choose the legacy profile door (null
  // route), distinct from no default and from the override-bypassing `local`.
  return {
    profile: saved.profile,
    route: saved.connectionId === null ? null : { connectionId: saved.connectionId, profile: saved.profile }
  }
}

export function prepareDefaultNewSession(): void {
  const target = defaultNewSessionTarget()

  if (!target) {
    return
  }

  if (target.route) {
    $newChatProfile.set(target.profile)
    $newChatRoute.set(target.route)
    captureNewChatSource(target.route.connectionId)
  } else {
    pinLegacyNewChatProfile(target.profile)
  }

  const activation = target.route
    ? ensureGatewayAgent(target.route.connectionId, target.profile)
    : ensureGatewayProfile(target.profile, { forceLegacyRoute: true })

  void activation.catch(error => {
    if (target.route) {
      notifyNewSessionTargetUnreachable(target.route, error)
    } else {
      notifyError(error, `Failed to open profile "${target.profile}"`)
    }
  })
}

/** [daveey] The saved new-session gateway did not answer. The draft stays
 *  bound to it (session.create never falls back to another gateway on its
 *  own); the notice offers this one session on This device instead, without
 *  changing the saved target. */
export function notifyNewSessionTargetUnreachable(route: AgentProfileRoute, error: unknown): string {
  const label =
    $connectionsRegistry.get()?.connections.find(connection => connection.id === route.connectionId)?.label ??
    route.connectionId

  const reason = error instanceof Error ? error.message : typeof error === 'string' ? error : ''

  return notify({
    id: `new-session-target-unreachable:${route.connectionId}`,
    kind: 'warning',
    title: translateNow('profiles.newSessionTargetUnreachable', label),
    message: translateNow('profiles.newSessionTargetUnreachableDetail'),
    ...(reason ? { detail: reason } : {}),
    ...(route.connectionId === LOCAL_CONNECTION_ID
      ? {}
      : {
          action: {
            label: translateNow('profiles.createOnThisDevice', thisDeviceLabel()),
            onClick: () => newSessionInAgent({ connectionId: LOCAL_CONNECTION_ID, profile: 'default' })
          }
        })
  })
}
