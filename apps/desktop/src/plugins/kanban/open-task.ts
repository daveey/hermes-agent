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

/** [daveey] Global name of the cross-plugin "open this card" hook. */
export const OPEN_TASK_HOOK = '__hermesKanbanOpenTask'

type OpenTaskHook = (taskId: string, board?: null | string, gateway?: null | string) => boolean

export interface OpenTaskHookDeps {
  navigate: (path: string) => void
  /** Seed the task's owning gateway + board (api.ts `rememberTaskOwner`). */
  rememberOwner?: (taskId: string, board: string, gateway: string) => void
}

/**
 * [daveey] Expose `window.__hermesKanbanOpenTask(taskId, board?, gateway?)` so
 * disk plugins (task-mentions' popup) can open a card's drawer, where it can be
 * edited and commented on, without importing this module. `gateway` is the
 * owning connection id for a card on another gateway. Parks the request and
 * navigates to /kanban; returns false for an invalid id. The returned disposer
 * removes the hook only if it is still ours.
 */
export function installOpenTaskHook(
  deps: OpenTaskHookDeps,
  target: Record<string, unknown> = globalThis as unknown as Record<string, unknown>
): () => void {
  const hook: OpenTaskHook = (taskId, board, gateway) => {
    if (typeof taskId !== 'string' || !taskId) {
      return false
    }

    const slug = typeof board === 'string' ? board : ''

    if (typeof gateway === 'string' && gateway) {
      deps.rememberOwner?.(taskId, slug, gateway)
    }

    $openTaskRequest.set({ board: slug, taskId })
    deps.navigate('/kanban')

    return true
  }

  target[OPEN_TASK_HOOK] = hook

  return () => {
    if (target[OPEN_TASK_HOOK] === hook) {
      delete target[OPEN_TASK_HOOK]
    }
  }
}
