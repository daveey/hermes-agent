import type { BackgroundUpdateCopy } from './types_background_update'

export const enBackgroundUpdate: BackgroundUpdateCopy = {
  updating: 'updating',
  dueIn: duration => `update in ${duration}`,
  blocked: 'update blocked',
  tooltipBatching: time => `A Hermes update is batched and applies at ${time}.`,
  tooltipDraining: elapsed =>
    `Hermes is updating: pausing running work before the swap (${elapsed} so far). Messages you send wait and go out when it finishes.`,
  tooltipPastCap: cap => `Past its ${cap} target: still waiting for running turns to reach a safe step.`,
  tooltipApplying: elapsed =>
    `Hermes is installing the update (${elapsed} since it was due). Messages you send go out when it finishes.`,
  tooltipBlocked: 'A Hermes update is owed but cannot apply right now.',
  waitingOn: items => `Waiting on: ${items}`
}
