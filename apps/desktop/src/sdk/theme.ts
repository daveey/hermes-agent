/** Live accent override — set a hex and the ACTIVE theme repaints with its
 *  accent family re-seeded from it (see `retintTheme`); `null` restores the
 *  authored palette. Deliberately not persisted: it is an authoring knob, not
 *  a setting, so a plugin that sets it must clear it on dispose. */
export { $accentOverride, setAccentOverride } from '@/themes/accent-override'
/** OKLCH colour maths, for anything deriving a palette rather than hardcoding
 *  one: perceptual conversion, the sRGB gamut boundary, and hue-stable
 *  blending. `readableOn` is the SDK's public name for the desktop's ink pick
 *  (`#161616` or `#ffffff`, whichever measures better on the background). */
export {
  hexToOklch,
  hueDelta,
  maxChroma,
  mixOklab,
  normalizeHex,
  type Oklch,
  oklchToHex,
  oklchToSrgb255,
  readableInk as readableOn
} from '@/themes/color'
/** The painted theme, its name, and the appearance it resolved to — plus
 *  `setTheme` / `setMode` to change it from a component. */
export { useTheme } from '@/themes/context'
/** Switch the theme from outside React (a gateway event, a connection coming
 *  up, any callback with no component around it). Returns false and leaves the
 *  appearance alone when the name doesn't resolve, so it doubles as the "is
 *  this theme installed?" check. */
export { requestTheme } from '@/themes/request'
export { retintTheme, themeHue } from '@/themes/retint'
export type { DesktopTheme, DesktopThemeColors } from '@/themes/types'
export { THEMES_AREA } from '@/themes/user-themes'
