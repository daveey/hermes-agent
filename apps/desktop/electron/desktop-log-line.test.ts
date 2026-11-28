import { afterEach, describe, expect, it } from 'vitest'

import { formatDesktopLogLine, formatLogStamp, formatUtcOffset } from './desktop-log-line'

const LINE_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3}) ([+-]\d{2}:\d{2}) \[hermes\] (.*)$/

const originalTz = process.env.TZ

afterEach(() => {
  if (originalTz === undefined) {
    delete process.env.TZ
  } else {
    process.env.TZ = originalTz
  }
})

/** The instant a stamp names, from its own local fields + offset only. */
const instantOf = (stamp: string) => {
  const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),(\d{3}) ([+-]\d{2}:\d{2})$/.exec(stamp)!

  return Date.parse(`${m[1]}T${m[2]}.${m[3]}${m[4]}`)
}

describe('formatDesktopLogLine', () => {
  it('stamps lines with local wall-clock time plus the numeric UTC offset', () => {
    process.env.TZ = 'America/Los_Angeles'
    const moment = new Date(2026, 8, 3, 7, 5, 9, 42)
    const line = formatDesktopLogLine('[boot] Resolving Hermes backend', formatLogStamp(moment))

    expect(line).toBe('2026-09-03 07:05:09,042 -07:00 [hermes] [boot] Resolving Hermes backend')
    expect(LINE_RE.exec(line)).not.toBeNull()
  })

  it('uses the standard-time offset in winter', () => {
    process.env.TZ = 'America/Los_Angeles'
    expect(formatLogStamp(new Date(2026, 10, 5, 12, 0, 0, 0))).toBe('2026-11-05 12:00:00,000 -08:00')
  })

  it('handles zones east of UTC and half-hour offsets', () => {
    process.env.TZ = 'Asia/Kolkata'
    expect(formatUtcOffset(new Date(2026, 0, 1))).toBe('+05:30')
    process.env.TZ = 'UTC'
    expect(formatUtcOffset(new Date(2026, 0, 1))).toBe('+00:00')
  })

  it('stays correct across local midnight: date and offset come from one Date', () => {
    process.env.TZ = 'America/Los_Angeles'
    const before = new Date('2026-11-05T07:59:59.999Z') // 23:59:59.999 -08:00 on Nov 4
    const after = new Date('2026-11-05T08:00:00.000Z') // 00:00:00.000 -08:00 on Nov 5

    expect(formatLogStamp(before)).toBe('2026-11-04 23:59:59,999 -08:00')
    expect(formatLogStamp(after)).toBe('2026-11-05 00:00:00,000 -08:00')
    expect(instantOf(formatLogStamp(before))).toBe(before.getTime())
    expect(instantOf(formatLogStamp(after))).toBe(after.getTime())
  })

  it('keeps the two instants of a DST fold distinct', () => {
    process.env.TZ = 'America/Los_Angeles'
    const first = new Date('2026-11-01T08:30:00.000Z') // 01:30 PDT
    const second = new Date('2026-11-01T09:30:00.000Z') // 01:30 PST

    expect(formatLogStamp(first)).toBe('2026-11-01 01:30:00,000 -07:00')
    expect(formatLogStamp(second)).toBe('2026-11-01 01:30:00,000 -08:00')
    expect(instantOf(formatLogStamp(first))).toBe(first.getTime())
    expect(instantOf(formatLogStamp(second))).toBe(second.getTime())
  })

  it('jumps the offset across the spring-forward gap', () => {
    process.env.TZ = 'America/Los_Angeles'
    const beforeGap = new Date('2026-03-08T09:59:59.000Z') // 01:59:59 PST
    const afterGap = new Date('2026-03-08T10:00:00.000Z') // 03:00:00 PDT

    expect(formatLogStamp(beforeGap)).toBe('2026-03-08 01:59:59,000 -08:00')
    expect(formatLogStamp(afterGap)).toBe('2026-03-08 03:00:00,000 -07:00')
    expect(instantOf(formatLogStamp(afterGap))).toBe(afterGap.getTime())
  })

  it('round-trips sampled instants across the year in several zones', () => {
    for (const tz of [
      'America/Los_Angeles',
      'America/Cancun',
      'Europe/London',
      'Asia/Kolkata',
      'Australia/Lord_Howe'
    ]) {
      process.env.TZ = tz

      for (let t = Date.UTC(2026, 0, 1); t < Date.UTC(2027, 0, 1); t += 37 * 60 * 1000 + 123) {
        expect(instantOf(formatLogStamp(new Date(t)))).toBe(t)
      }
    }
  })
})
