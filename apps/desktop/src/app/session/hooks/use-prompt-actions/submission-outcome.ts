import { type PreparedSubmission, readPreparedSubmission, writePreparedSubmission } from './prepared-submissions'

/**
 * What became of a journaled send that did not land, recorded on its journal entry so a later
 * retry of the same words (the composer's restored draft, same identity) can say what it knows.
 * Only `unknown` (or no record: a window that died mid-send, an earlier build) makes the retry
 * say "Could not confirm the message was sent"; a definite refusal or an abort before the
 * submit left means nothing was sent. An outcome is never downgraded: once any attempt of the
 * send ended unknown (it may have been admitted), every later outcome of it is `unknown` too.
 */
export type SubmissionOutcome = 'aborted' | 'refused' | 'unknown'

/** The retry's notice may claim nothing about the earlier attempt only when its outcome is unknown. */
export function sendUnconfirmed(retained: PreparedSubmission | undefined): boolean {
  return retained !== undefined && retained.outcome !== 'refused' && retained.outcome !== 'aborted'
}

/** Journal `entry` for the attempt about to start: until it ends, its outcome is unknown. An earlier
 *  attempt of the same send journaled under `key` that ended unknown (or never recorded an end) stays
 *  counted: `earlierUnknown` keeps every later outcome `unknown`. */
export async function writeInFlightSubmission(key: string, entry: PreparedSubmission): Promise<void> {
  // A journal that cannot be read falls back to what this window holds: the retained entry it adopted
  // carries the outcome recorded for the earlier attempts (a fresh send carries none).
  const earlier = await readPreparedSubmission(key).catch(() =>
    entry.earlierUnknown || entry.outcome === 'unknown' ? entry : undefined)

  if (earlier?.id === entry.id && (earlier.earlierUnknown || sendUnconfirmed(earlier))) {
    entry.earlierUnknown = true
  }

  delete entry.outcome

  return writePreparedSubmission(key, entry)
}

async function recordOutcome(key: string, entry: PreparedSubmission, outcome: SubmissionOutcome): Promise<void> {
  entry.outcome = entry.earlierUnknown ? 'unknown' : outcome
  // A failed save leaves the entry unknown, which only makes a later notice more careful.
  await writePreparedSubmission(key, entry).catch(error => console.warn('[prepared-submission-outcome]', error))
}

/** The user switched chats after the entry was journaled and before the submit left: nothing was sent. */
export async function submissionAborted<T>(key: string, entry: PreparedSubmission, abort: () => T): Promise<T> {
  await recordOutcome(key, entry, 'aborted')

  return abort()
}

/**
 * The submit failed with `error`. A definite refusal (the owner answered that it admitted nothing)
 * is `refused`, unless an earlier try of this same submit went unanswered (`replayUncertain`):
 * then, like any other failure, it is `unknown`. Returns `error` for the caller to rethrow.
 */
export async function submissionFailed(
  key: string,
  entry: PreparedSubmission,
  error: unknown,
  { definite, replayUncertain }: { definite: (error: unknown) => boolean; replayUncertain: boolean }
): Promise<unknown> {
  await recordOutcome(key, entry, definite(error) && !replayUncertain ? 'refused' : 'unknown')

  return error
}

/**
 * Wraps the submit callback that a recovery wrapper (withSessionNotFoundResume, also on timeouts) may
 * call again. A call after an earlier one failed with anything but a definite refusal replays a submit
 * that may have been admitted: `onReplay` marks the send's outcome uncertain before it goes.
 */
export function noteUnansweredReplays<A extends unknown[], R>(
  run: (...args: A) => Promise<R>,
  definite: (error: unknown) => boolean,
  onReplay: () => void
): (...args: A) => Promise<R> {
  let unanswered = false

  return async (...args) => {
    if (unanswered) {
      onReplay()
    }

    try {
      return await run(...args)
    } catch (error) {
      unanswered ||= !definite(error)
      throw error
    }
  }
}
