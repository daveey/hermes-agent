/**
 * Desktop log line formatting shared by every desktop log surface:
 * `desktop.log`, the in-app "RECENT LOGS" view, and crash forensics.
 *
 * The stamp is local time in the `YYYY-MM-DD HH:MM:SS,mmm` shape of Python's
 * default `asctime` (agent.log, gui.log, errors.log), followed by the numeric
 * UTC offset of that same instant (`-08:00`). The offset makes each line an
 * unambiguous instant across DST folds or when the process zone differs from
 * the host's. Older installs still write lines without it; readers accept both
 * and must never guess a zone for the legacy shape.
 * `hermes logs desktop --since` parses these lines. See #84405 for why lines
 * carry a stamp at all.
 */

const pad = (value: number, width = 2) => String(value).padStart(width, '0')

/** `-08:00` / `+05:30`: the UTC offset in effect at `date`, read from the same Date as the local fields. */
export function formatUtcOffset(date: Date): string {
  const minutes = -Math.round(date.getTimezoneOffset()) || 0
  const sign = minutes < 0 ? '-' : '+'
  const abs = Math.abs(minutes)

  return `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** `2026-09-28 13:18:46,062 -07:00`: machine-local time plus its numeric UTC offset. */
export function formatLogStamp(date: Date): string {
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`

  return `${day} ${time},${pad(date.getMilliseconds(), 3)} ${formatUtcOffset(date)}`
}

/**
 * Format one desktop log line with a local-time stamp.
 *
 * `stamp` defaults to now; callers that batch multiple lines (a single
 * stdout chunk) pass one shared stamp so the group reads as one event.
 */
export function formatDesktopLogLine(text: string, stamp = formatLogStamp(new Date())): string {
  return `${stamp} [hermes] ${text}`
}
