// Proves the E2E driver entry points resolve every import under the
// runtime-only tests-js install the release workflows use:
//   pnpm install --frozen-lockfile --filter "{./tests-js}..." --prod --ignore-scripts
// That install has no devDependencies (vitest, jsdom, node-pty, ...), so a
// driver that imports one only fails on a hosted macOS/Windows release runner.
// This walks each entry point's static import graph (import/export-from,
// import(), require() with a string literal) and resolves every specifier the
// way node would from that file, against the installed tree.
//
// Usage: node .github/scripts/check-runtime-driver-imports.mjs [--list]
// Entry points are tests/install/e2e-assets/*.{mjs,cjs,ts} (minus .d.mts) and
// EXTRA_ENTRIES; tests/ci/test_runtime_install_shape.py keeps them in sync with
// what the workflows launch.
import fs from 'node:fs'
import { builtinModules, createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const ASSETS = 'tests/install/e2e-assets'
// Launched outside e2e-assets: desktop-smoke-windows.ps1 and mock-provider.sh run it.
export const EXTRA_ENTRIES = ['tests-js/scripts/mock-provider-config.ts']
const CODE = /\.(mjs|cjs|js|ts|mts|cts)$/
const builtins = new Set(builtinModules)

export function entryPoints() {
  const dir = path.join(root, ASSETS)
  const found = fs
    .readdirSync(dir)
    .filter(n => CODE.test(n) && !n.endsWith('.d.mts'))
    .map(n => `${ASSETS}/${n}`)

  return [...found, ...EXTRA_ENTRIES].sort()
}

const SPEC = [
  /\b(?:import|export)\s+(?!type\b)[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]/g,
  /\bimport\s*['"]([^'"]+)['"]/g,
  /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g
]

function specifiers(source) {
  const text = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const out = new Set()

  for (const re of SPEC) {
    for (const m of text.matchAll(re)) {
      out.add(m[1])
    }
  }

  return out
}

function bareName(spec) {
  const parts = spec.split('/')

  return spec.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0]
}

function resolveBare(spec, fromFile) {
  try {
    return createRequire(fromFile).resolve(spec)
  } catch (e) {
    if (e.code !== 'ERR_PACKAGE_PATH_NOT_EXPORTED' && e.code !== 'ERR_REQUIRE_ESM') {
      throw e
    }
  }

  // ESM-only package: require() cannot load it, but it is installed.
  for (let dir = path.dirname(fromFile); ; dir = path.dirname(dir)) {
    if (fs.existsSync(path.join(dir, 'node_modules', bareName(spec), 'package.json'))) {
      return dir
    }

    if (dir === path.dirname(dir)) {
      throw new Error(`Cannot find package '${bareName(spec)}'`)
    }
  }
}

function resolveRelative(spec, fromFile) {
  const base = path.resolve(path.dirname(fromFile), spec)
  const exts = ['.mjs', '.cjs', '.js', '.ts', '.mts', '.cts', '.json']

  for (const cand of [base, ...exts.map(e => base + e)]) {
    if (fs.existsSync(cand) && fs.statSync(cand).isFile()) {
      return cand
    }
  }

  throw new Error(`Cannot find file '${spec}'`)
}

export function check(entries) {
  const failures = []
  const seen = new Set()

  const visit = (file, via) => {
    if (seen.has(file)) {
      return
    }

    seen.add(file)

    if (!CODE.test(file)) {
      return
    }

    for (const spec of specifiers(fs.readFileSync(file, 'utf8'))) {
      try {
        if (spec.startsWith('node:') || builtins.has(spec)) {
          continue
        }

        if (spec.startsWith('.')) {
          visit(resolveRelative(spec, file), via)
        } else {
          resolveBare(spec, file)
        }
      } catch (e) {
        failures.push(`${path.relative(root, file)} (from ${via}): '${spec}': ${e.message.split('\n')[0]}`)
      }
    }
  }

  for (const rel of entries) {
    const file = path.join(root, rel)

    if (fs.existsSync(file)) {
      visit(file, rel)
    } else {
      failures.push(`${rel}: entry point missing`)
    }
  }

  return { failures, files: seen.size }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const entries = entryPoints()

  if (process.argv.includes('--list')) {
    console.log(entries.join('\n'))
  } else {
    const { failures, files } = check(entries)

    if (failures.length) {
      console.error(`${failures.length} unresolved import(s) under the runtime-only install:`)

      for (const f of failures) {
        console.error(`  ${f}`)
      }

      process.exit(1)
    }

    console.log(`${entries.length} driver entry points, ${files} files: every import resolves`)
  }
}
