/**
 * True only when a row was written after the live turn began. Text alone
 * cannot tell this turn's reply from the previous turn's answer to the same
 * resent prompt, so a missing timestamp (older runtime) never passes.
 */
export function committedDuringTurn(turnStartedAt: unknown, committedAt: unknown): boolean {
  return typeof turnStartedAt === 'number' && typeof committedAt === 'number' && committedAt >= turnStartedAt
}
