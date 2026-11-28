// Shared origin-wide exclusive lock fixture. Both request overloads are used
// by renderer consumers; blocking requests queue instead of failing a peer.
export function archiveQueueLocksFixture() {
  const held = new Set<string>()
  const waiters = new Map<string, Array<() => void>>()
  type Callback = (lock: Lock | null) => unknown

  const locks = {
    request: async (name: string, optionsOrCallback: LockOptions | Callback, callback?: Callback) => {
      const options = typeof optionsOrCallback === 'function' ? {} : optionsOrCallback
      const run = typeof optionsOrCallback === 'function' ? optionsOrCallback : callback!

      if (held.has(name)) {
        if (options.ifAvailable) {
          return run(null)
        }
        await new Promise<void>(resolve => {
          const queue = waiters.get(name) ?? []
          queue.push(resolve)
          waiters.set(name, queue)
        })
      } else {
        held.add(name)
      }

      try {
        return await run({ name, mode: 'exclusive' } as Lock)
      } finally {
        const queue = waiters.get(name)
        const next = queue?.shift()

        if (next) {
          next()
        } else {
          held.delete(name)
          waiters.delete(name)
        }
      }
    }
  }

  return { locks, held }
}
