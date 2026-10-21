// [daveey] Archive-hotkey handlers: let a plugin take over the ⌘⇧A
// "archive and advance" hotkey (`session.archive`) for the sessions it owns.
//
// Core's default archives the session through `archiveSession` and steps to
// the next sidebar session. That is wrong for sessions that live in a plugin's
// own list instead of the sidebar (e.g. Inbox messages: hidden from Recents,
// possibly on another gateway). A handler sees the target id first; returning
// true means "handled" and core does nothing else. To fall back to core's
// navigation after archiving its last item, the handler calls
// `openNextSession()`, which opens the session core itself would have picked.

export interface ArchiveHotkeyContext {
  /** The stored id the hotkey targets (the selected row, else the open chat). */
  sessionId: string
  /** Whether that session is the one open in the main view. */
  isFocused: boolean
  /** Open the session core's default would step to (next status-striped
   *  sidebar row, else the plain neighbour). No-op when there is none. */
  openNextSession: () => void
}

/** Return true when the handler archived (or deliberately ignored) the
 *  session; false/undefined lets the next handler, then core, run. */
export type ArchiveHotkeyHandler = (context: ArchiveHotkeyContext) => boolean | undefined | void

const handlers = new Map<string, ArchiveHotkeyHandler>()

/** Register a handler under a unique id; the disposer removes it. A second
 *  registration under the same id replaces the first. */
export function registerArchiveHotkeyHandler(id: string, handler: ArchiveHotkeyHandler): () => void {
  handlers.set(id, handler)

  return () => {
    if (handlers.get(id) === handler) {
      handlers.delete(id)
    }
  }
}

/** Offer the hotkey to each handler in registration order; true as soon as one
 *  claims it. A handler that throws is skipped, so a broken plugin can never
 *  break the hotkey. */
export function runArchiveHotkeyHandlers(context: ArchiveHotkeyContext): boolean {
  for (const handler of handlers.values()) {
    try {
      if (handler(context) === true) {
        return true
      }
    } catch (error) {
      console.warn('[archive-hotkey] handler failed', error)
    }
  }

  return false
}

/** The ⌘⇧A dispatch: offer the target to the handlers, else run core's
 *  `archive` with `onDeselect` (open the next sidebar session). Wiring passes
 *  `archiveSession`; split out so both paths are unit-testable. */
export function dispatchArchiveHotkey(
  sessionId: string,
  options: {
    focusedId: null | string | undefined
    onDeselect: (() => void) | undefined
    archive: (sessionId: string, options: { onDeselect?: () => void }) => unknown
  }
): 'handled' | 'core' {
  const { archive, focusedId, onDeselect } = options

  if (
    runArchiveHotkeyHandlers({
      isFocused: focusedId === sessionId,
      openNextSession: () => onDeselect?.(),
      sessionId
    })
  ) {
    return 'handled'
  }

  void archive(sessionId, { onDeselect })

  return 'core'
}

// [daveey] The same "step to the next session" outside the hotkey: a plugin
// that closes its own session from a button (Inbox resolve/task actions on
// the last card) must not leave the user on the now-archived chat. Wiring
// installs the opener (it owns `navigate`); plugins reach it through
// `ctx.openNextSession(fromId)`.

/** Opens the session core's ⌘⇧A would step to from `fromId`; false when
 *  there is none. */
export type NextSessionOpener = (fromId: string) => boolean

let nextSessionOpener: NextSessionOpener | null = null

export function registerNextSessionOpener(opener: NextSessionOpener): () => void {
  nextSessionOpener = opener

  return () => {
    if (nextSessionOpener === opener) {
      nextSessionOpener = null
    }
  }
}

/** The opener wiring installs: `pick` is core's next-session choice (the one
 *  ⌘⇧A uses), `open` navigates. Never re-opens `fromId` itself. */
export function nextSessionOpenerFor(
  pick: (fromId: string) => null | string | undefined,
  open: (sessionId: string) => unknown
): NextSessionOpener {
  return fromId => {
    const next = pick(fromId)

    if (!next || next === fromId) {
      return false
    }

    void open(next)

    return true
  }
}

/** Step from `fromId` like ⌘⇧A after an archive. False when the shell isn't
 *  mounted, there is no next session, or the opener throws. */
export function openNextSessionFrom(fromId: string): boolean {
  if (!fromId || !nextSessionOpener) {
    return false
  }

  try {
    return nextSessionOpener(fromId) === true
  } catch (error) {
    console.warn('[archive-hotkey] next-session opener failed', error)

    return false
  }
}

/** Test seam. */
export function resetArchiveHotkeyHandlersForTests(): void {
  handlers.clear()
  nextSessionOpener = null
}
