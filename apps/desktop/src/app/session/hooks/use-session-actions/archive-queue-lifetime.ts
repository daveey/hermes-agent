import { translateNow } from '@/i18n'
import type { Translations } from '@/i18n/types'
import { dismissNotification, notify } from '@/store/notifications'

import { holdArchiveQueueLock } from './archive-queue-lock'

const PREFIX = 'hermes:archive-preflight-queue:'
const active = new Set<string>()
const storageKeys = (storage: Storage) => Array.from({ length: storage.length }, (_, index) => storage.key(index) ?? '')

/** Locks, unlike URL window types or module memory, distinguish live renderers
 * from reload/restart orphans. Never retire a peer's still-running intent. */
export async function reportDroppedArchiveQueues(): Promise<void> {
  const dropped = () => notify({ id: 'archive-queue-reload', kind: 'warning', persistent: true, durationMs: 0,
    message: translateNow('desktop.archiveQueueDropped') })

  const legacy = storageKeys(sessionStorage).filter(key => key.startsWith(PREFIX) && !active.has(key))

  if (legacy.length) {dropped(); legacy.forEach(key => sessionStorage.removeItem(key))}

  if (!navigator.locks) {return}
  await Promise.all(storageKeys(localStorage).filter(key => key.startsWith(PREFIX) && !active.has(key)).map(key =>
    navigator.locks.request(key, { ifAvailable: true }, lock => {
      if (!lock || localStorage.getItem(key) === null) {return}
      dropped()
      localStorage.removeItem(key)
    })))
}

export function archiveQueueLifetime(attemptId: string, id: string, copy: Translations['desktop']) {
  const key = `${PREFIX}${attemptId}`
  let release: (() => void) | undefined
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
    enqueue: async () => {
      if (queued) {return false}
      release = await holdArchiveQueueLock(key, controller.signal)

      if (disposed || controller.signal.aborted) {
        release()
        controller.signal.throwIfAborted()
        throw new Error(copy.archiveQueueCancelled)
      }

      // If persistence is unavailable, fail visibly rather than silently losing
      // an intent on restart. The held lock, not a URL label, owns this marker.
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

      try {localStorage.removeItem(key)} finally {release?.()}

      if (queued) {dismissNotification(key)}
    }
  }
}
