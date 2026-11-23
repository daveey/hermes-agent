// Timeout is a recoverable observation, not a terminal boot outcome. Keep the
// failure visible between probes; only a usable descriptor resumes normal boot.
export class BootTimeoutRecovery {
  revision = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private delay = 2_000
  private retry: (() => void) | null = null

  schedule(retry: () => void): void {
    this.clearTimer()
    this.retry = retry
    this.timer = setTimeout(() => {
      this.timer = null
      this.retry = null
      retry()
    }, this.delay)
    this.delay = Math.min(this.delay * 2, 30_000)
  }

  expedite(): void {
    const retry = this.retry

    if (!retry) {
      return
    }
    this.clearTimer()
    this.retry = null
    retry()
  }

  stop(): void {
    this.clearTimer()
    this.retry = null
    this.delay = 2_000
  }

  invalidate(): void {
    this.stop()
    this.revision += 1
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer)
    }
    this.timer = null
  }
}

export { isBackendReadinessTimeout } from '@hermes/shared'
