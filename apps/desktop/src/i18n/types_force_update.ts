/** [daveey] Copy for "Force update" on the backend version pill's context menu: push a pending core update the
 *  host runs on its own (hermes-sync) through now, stopping running turns. Part of `Translations['shell']['statusbar']`. */
export interface ForceUpdateCopy {
  /** The context-menu item on the backend version pill. */
  menuItem: string
  confirmTitle: string
  /** Confirm body; `runningTurns` is how many turns the host runs right now (every surface), null when unknown. */
  confirmBody: (runningTurns: null | number) => string
  confirmLabel: string
  cancelLabel: string
  requested: string
  /** Toast title when the request (or the status read before it) failed. */
  failed: string
  /** [daveey] Update dialog after Update was handed to hermes-sync (it applies updates on this host). */
  syncedTitle: string
  /** Body when hermes-sync took the request: busy chats pause and continue after the update. */
  syncedApplying: string
}
