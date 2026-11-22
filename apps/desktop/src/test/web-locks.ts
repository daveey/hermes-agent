// Shared origin-wide lock fixture; separate module instances/renderers see the
// same manager, while a reload can release the old renderer's held callbacks.
export function archiveQueueLocksFixture() {
  const held = new Set<string>()

  const locks = {
    request: async (name: string, options: LockOptions, callback: (lock: Lock | null) => unknown) => {
      if (held.has(name)) {
        if (options.ifAvailable) {return callback(null)}
        throw new Error('duplicate fixture lock')
      }

      held.add(name)

      try {return await callback({ name, mode: 'exclusive' } as Lock)} finally {held.delete(name)}
    }
  }

  return { locks, held }
}
