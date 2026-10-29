/**
 * [daveey] What the renderer is told when the primary backend child exits.
 *
 * A planned restart (a core update — `hermes update` stops the Desktop-owned
 * backend while it holds the update marker — or this app's own update
 * hand-off) is not a crash: the payload carries `planned: 'update'` so the
 * renderer shows a quiet "Updating Hermes…" notice instead of the red
 * "stopped working in the background" card. Anything else, including a probe
 * that throws, is reported as unexpected: a real crash must never be hidden.
 */
export type BackendExitPayload = {
  code: number | null
  signal: string | null
  error?: string
  planned?: 'update'
}

export async function classifyBackendExit(
  payload: BackendExitPayload,
  isPlannedRestart: () => boolean | Promise<boolean>
): Promise<BackendExitPayload> {
  let planned = false

  try {
    planned = Boolean(await isPlannedRestart())
  } catch {
    planned = false
  }

  return planned ? { ...payload, planned: 'update' } : payload
}

type ExitWindow = {
  isDestroyed(): boolean
  webContents?: { isDestroyed(): boolean; send(channel: string, payload: BackendExitPayload): void } | null
}

/**
 * `sendBackendExit` for the primary window: silent while `isSilenced` (an intentional soft re-home), otherwise
 * classifies the exit and sends `hermes:backend-exit`. Returns the send's promise for tests; main ignores it.
 */
export function createBackendExitSender(deps: {
  isSilenced: () => boolean
  isPlannedRestart: () => boolean | Promise<boolean>
  window: () => ExitWindow | null | undefined
  log: (line: string) => void
}) {
  return (payload: BackendExitPayload): Promise<void> => {
    if (deps.isSilenced()) {
      return Promise.resolve()
    }

    return classifyBackendExit(payload, deps.isPlannedRestart).then(classified => {
      if (classified.planned) {
        deps.log('[supervisor] backend exit during a planned update; renderer shows the quiet updating notice')
      }

      const win = deps.window()

      if (!win || win.isDestroyed() || !win.webContents || win.webContents.isDestroyed()) {
        return
      }

      win.webContents.send('hermes:backend-exit', classified)
    })
  }
}
