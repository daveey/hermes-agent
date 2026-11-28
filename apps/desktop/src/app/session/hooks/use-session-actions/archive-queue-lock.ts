/** The callback remains pending until disposal or renderer death. Chromium
 * releases Web Locks on context destruction, including reload and app exit. */
export async function holdArchiveQueueLock(key: string, signal: AbortSignal): Promise<() => void> {
  if (!navigator.locks) {
    throw new Error('Archive queue persistence requires Web Locks')
  }
  let release!: () => void
  const held = new Promise<void>(resolve => {
    release = resolve
  })
  let acquired!: () => void
  let failed!: (error: unknown) => void
  const ready = new Promise<void>((resolve, reject) => {
    acquired = resolve
    failed = reject
  })
  void navigator.locks
    .request(key, { ifAvailable: true }, async lock => {
      if (!lock) {
        throw new Error('Archive queue already owned by another renderer')
      }
      signal.throwIfAborted()
      acquired()
      await held
    })
    .catch(failed)
  await ready

  return release
}
