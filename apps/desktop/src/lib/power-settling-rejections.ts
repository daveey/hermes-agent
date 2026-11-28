// [daveey] Background reads the main process skips while the Mac sleeps or settles after a wake reject with
// PowerSettlingError (electron/power-settle.ts). That is expected: the next poll retries. A renderer caller
// that fire-and-forgets such a read without a .catch left one "Uncaught (in promise) ... PowerSettlingError"
// console error per skipped read in desktop.log (4x around the 10-30 10:00 and 10:44 PDT wakes). Electron
// strips the error's `code` across IPC, so the only marker the renderer gets is the error name in the message.
// This guard swallows exactly those rejections. Any other unhandled rejection still reaches the console.

const POWER_SETTLING_MARKER = 'PowerSettlingError:'

export function isPowerSettlingRejection(reason: unknown): boolean {
  if (reason && typeof reason === 'object' && (reason as { code?: unknown }).code === 'POWER_SETTLING') {
    return true
  }

  const message = typeof reason === 'string' ? reason : String((reason as { message?: unknown } | null)?.message ?? '')

  return message.includes(POWER_SETTLING_MARKER)
}

interface RejectionTarget {
  addEventListener(type: 'unhandledrejection', listener: (event: PromiseRejectionEvent) => void): void
  removeEventListener(type: 'unhandledrejection', listener: (event: PromiseRejectionEvent) => void): void
}

/** Install once on window; returns an uninstaller (tests). */
export function installPowerSettlingRejectionGuard(target: RejectionTarget = window): () => void {
  const onRejection = (event: PromiseRejectionEvent) => {
    if (isPowerSettlingRejection(event.reason)) {
      event.preventDefault()
    }
  }

  target.addEventListener('unhandledrejection', onRejection)

  return () => target.removeEventListener('unhandledrejection', onRejection)
}
