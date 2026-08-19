import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

export const THEME_STORAGE_KEY = 'cms-theme'
export const THEME_COLOR_LIGHT = '#4c5fd5'
export const THEME_COLOR_DARK = '#10121a'

export function themeColorFor(isDark) {
  return isDark ? THEME_COLOR_DARK : THEME_COLOR_LIGHT
}

export function applyThemeColor(isDark, root = typeof document === 'undefined' ? null : document) {
  if (!root?.querySelectorAll) return
  const color = themeColorFor(isDark)
  for (const meta of root.querySelectorAll('meta[name="theme-color"]')) {
    meta.setAttribute('content', color)
  }
}

/**
 * Pure: map a mode plus the system preference to an effective theme.
 * Exported for unit tests; no DOM access.
 */
export function resolveThemeMode(mode, systemPrefersDark) {
  if (mode === 'dark') return 'dark'
  if (mode === 'light') return 'light'
  return systemPrefersDark ? 'dark' : 'light'
}

export function readSystemPrefersDark() {
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  }
  return false
}

export function loadThemeMode() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'light' || stored === 'dark') return stored
  } catch (_) {
    /* localStorage unavailable (private mode) -> follow system */
  }
  return 'system'
}

export function useTheme() {
  const mode = ref(loadThemeMode())
  // Read synchronously in setup so the first naive-ui render already uses
  // the right theme (no flash of the wrong one); the media query listener
  // below keeps it in sync afterwards.
  const systemPrefersDark = ref(readSystemPrefersDark())
  // Boolean: is the effective theme dark? (resolveThemeMode returns the
  // 'light'/'dark' string; truthiness checks elsewhere rely on a real bool.)
  const isDark = computed(() => resolveThemeMode(mode.value, systemPrefersDark.value) === 'dark')

  let mql = null

  function apply() {
    if (typeof document === 'undefined') return
    const dark = isDark.value
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    applyThemeColor(dark)
  }

  watch(isDark, apply)

  function syncFromSystem() {
    systemPrefersDark.value = readSystemPrefersDark()
  }

  function setMode(next) {
    mode.value = next
    try {
      if (next === 'system') localStorage.removeItem(THEME_STORAGE_KEY)
      else localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch (_) { /* ignore storage failures */ }
    apply()
  }

  /** Toggle between the opposite effective theme and back to system. */
  function toggle() {
    if (mode.value === 'system') setMode(isDark.value ? 'light' : 'dark')
    else setMode('system')
  }

  onMounted(() => {
    mql = window.matchMedia('(prefers-color-scheme: dark)')
    mql.addEventListener('change', syncFromSystem)
    apply()
  })

  onUnmounted(() => {
    if (mql) mql.removeEventListener('change', syncFromSystem)
  })

  return { mode, isDark, setMode, toggle }
}
