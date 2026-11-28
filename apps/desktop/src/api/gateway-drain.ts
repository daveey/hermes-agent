/** Explicit maintenance/projection refusal, not a generic owner timeout.
 * Electron may wrap an HTTP error in its IPC message; retain the producer's
 * drain marker rather than treating every 503 as a restart. */
export function isGatewayDrainError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  const reason =
    error && typeof error === 'object' ? (error as { data?: { reason?: unknown } }).data?.reason : undefined

  return (
    reason === 'runtime_draining' ||
    reason === 'gateway_draining' ||
    /owner_unavailable:\s*(?:draining|starting)\b|\bgateway (?:is )?draining\b|\bruntime_draining\b|\bgateway_draining\b|\bsessions projection unavailable\b/i.test(
      message
    )
  )
}
