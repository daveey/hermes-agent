import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

const STORAGE_KEY = 'hermes.desktop.completionSoundVariantId'

export const DEFAULT_COMPLETION_SOUND_VARIANT_ID = 1

// 0 = Off (no completion sound). 1–14 are the curated presets in
// lib/completion-sound.ts. Validating by range (not membership) keeps this
// store free of a dependency on the lib, which imports the atom back.
export const COMPLETION_SOUND_OFF_ID = 0

// Range mirrors COMPLETION_SOUND_VARIANTS in lib/completion-sound.ts. Validating
// by range (not membership) keeps this store free of a dependency on the lib,
// which imports the atom back — a membership check would close that cycle.
const VARIANT_COUNT = 14

export function isCompletionSoundOff(variantId: number): boolean {
  return variantId === COMPLETION_SOUND_OFF_ID
}

export function resolveCompletionSoundVariantId(variantId: number): number {
  if (variantId === COMPLETION_SOUND_OFF_ID) {
    return COMPLETION_SOUND_OFF_ID
  }

  return Number.isInteger(variantId) && variantId >= 1 && variantId <= VARIANT_COUNT
    ? variantId
    : DEFAULT_COMPLETION_SOUND_VARIANT_ID
}

function load(): number {
  const stored = storedString(STORAGE_KEY)

  return stored ? resolveCompletionSoundVariantId(Number.parseInt(stored, 10)) : DEFAULT_COMPLETION_SOUND_VARIANT_ID
}

export const $completionSoundVariantId = atom(load())

$completionSoundVariantId.subscribe(id => persistString(STORAGE_KEY, String(id)))

export function setCompletionSoundVariantId(variantId: number) {
  $completionSoundVariantId.set(resolveCompletionSoundVariantId(variantId))
}
