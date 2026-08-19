import assert from 'node:assert/strict'
import test from 'node:test'
import { applyThemeColor, loadThemeMode, resolveThemeMode, themeColorFor, useTheme } from '../src/useTheme.js'

// Regression guard: isDark must be a real boolean. The bare theme string
// ('light'/'dark') is always truthy, which broke both apply() and toggle()
// (page was forced dark and the toggle could not switch to light).
test('useTheme exposes isDark as a boolean', () => {
  const originalWarn = console.warn
  console.warn = () => {}
  try {
    const { isDark } = useTheme()
    assert.equal(typeof isDark.value, 'boolean')
  } finally {
    console.warn = originalWarn
  }
})

test('resolveThemeMode maps explicit modes directly', () => {
  assert.equal(resolveThemeMode('dark', false), 'dark')
  assert.equal(resolveThemeMode('dark', true), 'dark')
  assert.equal(resolveThemeMode('light', true), 'light')
  assert.equal(resolveThemeMode('light', false), 'light')
})

test('resolveThemeMode follows system preference in system mode', () => {
  assert.equal(resolveThemeMode('system', true), 'dark')
  assert.equal(resolveThemeMode('system', false), 'light')
})

test('loadThemeMode reads a stored explicit mode', () => {
  globalThis.localStorage = { getItem: () => 'dark' }
  assert.equal(loadThemeMode(), 'dark')
  delete globalThis.localStorage
})

test('loadThemeMode falls back to system when nothing is stored', () => {
  globalThis.localStorage = { getItem: () => null }
  assert.equal(loadThemeMode(), 'system')
  delete globalThis.localStorage
})

test('loadThemeMode ignores invalid stored values', () => {
  globalThis.localStorage = { getItem: () => 'neon' }
  assert.equal(loadThemeMode(), 'system')
  delete globalThis.localStorage
})

test('theme-color follows the effective theme, not only prefers-color-scheme', () => {
  assert.equal(themeColorFor(false), '#4c5fd5')
  assert.equal(themeColorFor(true), '#10121a')
  const metas = [
    { name: 'theme-color', content: '#4c5fd5', media: '', setAttribute(name, value) { this[name] = value } },
    { name: 'theme-color', content: '#10121a', media: '(prefers-color-scheme: dark)', setAttribute(name, value) { this[name] = value } },
  ]
  const root = { querySelectorAll: (sel) => sel === 'meta[name="theme-color"]' ? metas : [] }
  applyThemeColor(true, root)
  assert.equal(metas[0].content, '#10121a')
  assert.equal(metas[1].content, '#10121a')
  applyThemeColor(false, root)
  assert.equal(metas[0].content, '#4c5fd5')
  assert.equal(metas[1].content, '#4c5fd5')
})
