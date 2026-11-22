import { translateNow } from '@/i18n'
import type { Translations } from '@/i18n/types'
import { dismissNotification, notify } from '@/store/notifications'

const PREFIX = 'hermes:archive-preflight-queue:'
const active = new Set<string>()
const storageKeys = (storage: Storage) => Array.from({ length: storage.length }, (_, index) => storage.key(index) ?? '')
const windowPrefix = () => `${PREFIX}${encodeURIComponent(new URLSearchParams(window.location.search).get('win') ?? 'main')}:`

/** Persist the existence, not a safety verdict or a mutation to replay. A new
 * renderer explicitly drops its window's old intents with a standing notice. */
export function reportDroppedArchiveQueues(): void {
  const legacy = storageKeys(sessionStorage).filter(key => key.startsWith(PREFIX) && !active.has(key))
  const keys = storageKeys(localStorage).filter(key => key.startsWith(windowPrefix()) && !active.has(key))

  if (!keys.length && !legacy.length) {return}
  notify({ id: 'archive-queue-reload', kind: 'warning', persistent: true, durationMs: 0,
    message: translateNow('desktop.archiveQueueDropped') })
  keys.forEach(key => localStorage.removeItem(key))
  legacy.forEach(key => sessionStorage.removeItem(key))
}

export function archiveQueueLifetime(attemptId: string, id: string, copy: Translations['desktop']) {
  const key = `${windowPrefix()}${attemptId}`
  let queued = false
  let disposed = false
  const controller = new AbortController()
  let cancel!: (reason: Error) => void
  const cancelled = new Promise<never>((_resolve, reject) => {cancel = reject})
  // The cancellation promise can outlive a successfully completed race.
  void cancelled.catch(() => {})

  const onCancel = () => {
    if (disposed) {return}
    const error = new Error(copy.archiveQueueCancelled)
    controller.abort(error)
    cancel(error)
  }

  active.add(key)

  return {
    signal: controller.signal,
    run: <T>(read: () => Promise<T>) => Promise.race([cancelled, Promise.resolve().then(read)]),
    sleep: async (ms: number) => {
      let timer: ReturnType<typeof setTimeout> | undefined

      try {
        await Promise.race([cancelled, new Promise<void>(resolve => {timer = setTimeout(resolve, ms)})])
      } finally {
        clearTimeout(timer)
      }
    },
    enqueue: () => {
      if (queued) {return false}
      // If persistence is unavailable, fail visibly rather than silently losing
      // an intent on restart. Window-scoped keys don't retire other windows' work.
      localStorage.setItem(key, id)
      queued = true
      notify({ id: key, kind: 'info', persistent: true, durationMs: 0,
        message: copy.archiveQueued, detail: id,
        action: { label: copy.archiveLinkedCancel, onClick: onCancel }, onDismiss: onCancel })

      return true
    },
    dispose: () => {
      disposed = true
      active.delete(key)
      localStorage.removeItem(key)

      if (queued) {dismissNotification(key)}
    }
  }
}
