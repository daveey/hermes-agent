/** [daveey] Version-pill copy for a core update the host runs on its own (hermes-sync), not one Desktop started.
 *  Part of `Translations['shell']['statusbar']`. Durations arrive pre-formatted (`45s`, `12m`, `1h 5m`). */
export interface BackgroundUpdateCopy {
  /** Label suffix while the update pauses work or installs. */
  updating: string
  /** Label suffix while the update waits for its batch window. */
  dueIn: (duration: string) => string
  /** Label suffix when an update is owed but nothing can apply it. */
  blocked: string
  tooltipBatching: (time: string) => string
  tooltipDraining: (elapsed: string) => string
  tooltipPastCap: (cap: string) => string
  tooltipApplying: (elapsed: string) => string
  tooltipBlocked: string
  waitingOn: (items: string) => string
}
