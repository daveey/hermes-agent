/** Foreground dials may use the pool's reserved slot; background stays implicit. */
export type SpawnPriority = 'foreground' | 'background'

export function dialPriority(spawnPriority: SpawnPriority): { priority: 'foreground' } | Record<never, never> {
  return spawnPriority === 'foreground' ? { priority: 'foreground' } : {}
}
