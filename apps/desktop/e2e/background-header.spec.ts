import { readFileSync } from 'node:fs'

import { expect, test } from '@playwright/test'
import { compile } from 'tailwindcss'

const read = (name: string) => readFileSync(new URL(`../src/app/chat/sidebar/${name}`, import.meta.url), 'utf8')
const section = read('background-section.tsx')
const header = read('sessions-section.tsx')
const squares = read('session-row-card-squares.tsx')
const headerClass = /className="([^"]*group\/section flex[^"]*)"/.exec(header)![1]
const actionClass = /className="([^"]+)" data-background-counts/.exec(section)?.[1]
const badgeClass = /className={`([^`]+)\$\{OUTLINES\[colour\]\}`}/.exec(squares)?.[1]
const groupClass = /cn\(\s*'([^']+)',\s*counts && '([^']+)'\s*\)/.exec(squares)

for (const width of [180, 240, 400]) {
  test(`Background header counts align right without clipping at ${width}px`, async ({ page }) => {
    expect(actionClass, 'aggregate must live in the trailing header action, not labelMeta').toBeTruthy()
    expect(badgeClass, 'counts must be outlined badges, not filled pips').toBeTruthy()
    expect(groupClass).toBeTruthy()
    const classes = `${headerClass} ${actionClass} ${badgeClass} ${groupClass![1]} ${groupClass![2]}`
    const theme = readFileSync(new URL('../node_modules/tailwindcss/theme.css', import.meta.url), 'utf8')
    const compiler = await compile(`${theme}\n@tailwind utilities;`)
    const css = compiler.build(classes.split(/\s+/))
    await page.setContent(`<style>${css}
      * { box-sizing: border-box; } body { margin: 0; font: 11px sans-serif; }
      #section { width: ${width}px; } #label { flex-shrink: 0; width: 100px; }
      #row { height: 25px; padding-right: 8px; text-align: right; }
    </style><div id="section"><div class="${headerClass}">
      <button id="label">BACKGROUND 20</button><div id="action" class="${actionClass}">
      <span id="counts" class="${groupClass![1]} ${groupClass![2]}">
        ${[6, 1, 1, 3, 110, 12, 8].map(n => `<span class="${badgeClass}">${n}</span>`).join('')}
      </span></div></div><div id="row"><span id="tail">110</span></div></div>`)

    const geometry = await page.locator('#section').evaluate(el => {
      const rect = (selector: string) => el.querySelector(selector)!.getBoundingClientRect()
      const label = rect('#label')
      const action = rect('#action')

      const badges = [...el.querySelectorAll('#counts > span')].map(b => {
        const r = b.getBoundingClientRect()

        return {
          left: r.left,
          right: r.right,
          width: r.width,
          height: r.height,
          scroll: b.scrollWidth,
          client: b.clientWidth
        }
      })

      return {
        labelRight: label.right,
        actionLeft: action.left,
        actionRight: action.right,
        right: rect('#tail').right,
        badges,
        scroll: el.scrollWidth,
        client: el.clientWidth
      }
    })

    expect(geometry.labelRight).toBeLessThanOrEqual(geometry.actionLeft)
    expect(Math.max(...geometry.badges.map(b => b.right))).toBeCloseTo(geometry.right, 0)
    expect(geometry.scroll).toBe(geometry.client)

    for (const badge of geometry.badges) {
      expect(badge.left).toBeGreaterThanOrEqual(geometry.actionLeft)
      expect(badge.width).toBeGreaterThanOrEqual(badge.height)
      expect(badge.scroll).toBeLessThanOrEqual(badge.client)
    }

    await page.screenshot({ path: `test-results/background-${width}.png` })
  })
}
