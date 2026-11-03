import crypto from 'node:crypto'

/**
 * Why a present remote dashboard lockfile was not reused, logged as one `reuse miss:` line so
 * misses can be counted from the lifecycle log. Split out of remote-lifecycle.ts (its size ratchet).
 */

// Fingerprint a token for the lockfile — never store the raw secret on the
// remote. SHA256, truncated.
export function fingerprintToken(token) {
  return crypto
    .createHash('sha256')
    .update(String(token || ''))
    .digest('hex')
    .slice(0, 32)
}

// Log why a present lockfile was not reused, so reuse misses can be counted from the lifecycle log.
export function logReuseMiss(log: (line: string) => void, lock, ctx) {
  if (!ctx.reusable) {
    log(`reuse miss: ${reuseMissReasons(lock, ctx).join('; ') || 'unknown'}`)
  }
}

// Why a present lockfile was not reused. Empty when every reuse condition holds.
function reuseMissReasons(lock, { pidAlive, owned, profile, reuseToken, hermesPath, hermesHome }): string[] {
  const reasons: string[] = []

  if (!pidAlive) {
    reasons.push(`pid ${lock.pid} not alive`)
  } else if (!owned) {
    reasons.push(`pid ${lock.pid} is not our dashboard`)
  }

  if (!(lock.port > 0)) {
    reasons.push('no port recorded')
  }

  if (lock.profile !== profile) {
    reasons.push(`profile ${lock.profile || "''"} != ${profile || "''"}`)
  }

  if (!reuseToken) {
    reasons.push('no stored token')
  } else if (lock.tokenFingerprint !== fingerprintToken(reuseToken)) {
    reasons.push('token fingerprint mismatch')
  }

  if (lock.hermesPath !== hermesPath) {
    reasons.push(`hermes path ${lock.hermesPath} != ${hermesPath}`)
  }

  if (lock.hermesHome !== hermesHome) {
    reasons.push(`hermes home ${lock.hermesHome} != ${hermesHome}`)
  }

  return reasons
}
