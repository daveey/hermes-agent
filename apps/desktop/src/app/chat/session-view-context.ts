import { createContext, useContext } from 'react'

import type { SessionView } from './session-view'

/**
 * The SessionView context object, kept free of store imports so leaf renderers
 * (transcript chips/links) can read which chat they sit in without pulling the
 * whole session store graph into their module. `null` = no provider = the
 * primary (main) chat; `useSessionView` in `session-view.tsx` maps that to
 * `PRIMARY_SESSION_VIEW`.
 */
export const SessionViewContext = createContext<null | SessionView>(null)

/** The enclosing tile's view, or null inside the main chat. */
export const useTileSessionView = (): null | SessionView => useContext(SessionViewContext)
