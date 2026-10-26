// [daveey] Age of a hermes-hub replica stamp, for cached (offline) sidebar rows.
/** "synced 5m ago" from an epoch-seconds stamp. */
export function syncedAgoLabel(syncedAt: null | number | undefined, nowMs: number = Date.now()): string {
  if (!syncedAt) {
    return 'never synced'
  }

  const seconds = Math.max(0, nowMs / 1000 - syncedAt)

  if (seconds < 60) {
    return 'synced just now'
  }

  if (seconds < 3600) {
    return `synced ${Math.floor(seconds / 60)}m ago`
  }

  if (seconds < 86_400) {
    return `synced ${Math.floor(seconds / 3600)}h ago`
  }

  return `synced ${Math.floor(seconds / 86_400)}d ago`
}
