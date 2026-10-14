/**
 * A "show me this task" request raised from outside the board page (the
 * completion toast's Open action). The caller parks {board, taskId} here and
 * navigates to /kanban; the page switches to that board, opens the task's
 * drawer, then clears the request so a later remount can't reopen it.
 *
 * Kept in its own module (not ui.tsx / api.ts) so completion-notify can raise
 * it without pulling React or creating an api.ts import cycle.
 */

import { atom } from '@hermes/plugin-sdk'

export interface OpenTaskRequest {
  board: string
  taskId: string
}

export const $openTaskRequest = atom<null | OpenTaskRequest>(null)
