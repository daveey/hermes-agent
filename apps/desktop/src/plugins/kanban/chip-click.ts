// The status-bar task pill opens the board. A plugin that has something better to show for that click (a panel
// about the same cards) can take it over: the pill first fires a cancellable window event, and opens the board
// only if nobody called preventDefault(). Core names no plugin; a plugin that takes the click over is expected to
// offer its own way to the board.
export const KANBAN_CHIP_EVENT = 'hermes-kanban-chip'

/** Fire the event; true = nobody took the click over, so the caller opens the board. */
export function chipClickOpensBoard(target: EventTarget = window): boolean {
  return target.dispatchEvent(new Event(KANBAN_CHIP_EVENT, { cancelable: true }))
}
