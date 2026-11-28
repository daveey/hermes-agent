import { randomUuid } from '@hermes/shared'

export type ArchiveTrace = ((stage: string) => void) & { attemptId: string }

/** One user invocation, including its automatic retries. Never log prompts or tokens. */
export function startArchiveTrace(sessionId: string): ArchiveTrace {
  const attemptId = randomUuid()

  const trace = Object.assign(
    (stage: string) => {
      const line = `[archive-trace] ${sessionId} ${attemptId} ${stage.replace(/[\r\n]/g, ' ').slice(0, 400)}`

      try {
        console.info(line)
        window.hermesDesktop?.logLine?.(line)
      } catch {
        // Diagnostics cannot change the mutation outcome.
      }
    },
    { attemptId }
  )

  trace('start')

  return trace
}
