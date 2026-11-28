import type { NewChatWorkspaceTarget } from '@/store/session'

export interface FreshSessionDraftOptions {
  preserveRoute?: boolean
  replaceRoute?: boolean
  rotateFreshDraftKey?: boolean
  workspaceTarget?: NewChatWorkspaceTarget
}

export function normalizeNewChatWorkspaceTarget(target: NewChatWorkspaceTarget): NewChatWorkspaceTarget {
  return typeof target === 'string' ? target.trim() || null : target
}

/** True when a fresh draft answers a user's New session action. The boolean form (`true` = replace the route) is
 *  the teardown other flows use after a delete, archive or dead-id recovery, and `rotateFreshDraftKey: false` /
 *  `preserveRoute` are the route-resume and dashboard-request forms: none of them is a click to time. */
export function isUserFreshDraft(options: boolean | FreshSessionDraftOptions): boolean {
  if (typeof options === 'boolean') {
    return !options
  }

  return options.rotateFreshDraftKey !== false && options.preserveRoute !== true
}
