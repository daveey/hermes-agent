/** Readiness timeouts are observations, not proof of a broken local install. */
export function isBackendReadinessTimeout(error: unknown, stage = 'resolving'): boolean {
  return (
    stage === 'resolving' && error instanceof Error && (
      /timed out.*(?:Hermes backend|backend port)/i.test(error.message)
      || /^Gateway starting \(deadline\)\./.test(error.message)
    )
  )
}
