import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Local development only: set VITE_API_PROXY to the demo backend URL so the
// Vite dev server can serve /login and /api/* through it (see
// scripts/dev_ui_smoke.py). Left unset, the build and CI are unaffected.
const apiTarget = process.env.VITE_API_PROXY

export default defineConfig({
  base: '/app/',
  plugins: [vue()],
  server: apiTarget
    ? {
        proxy: {
          '/login': { target: apiTarget, changeOrigin: true },
          '/api': { target: apiTarget, changeOrigin: true },
        },
      }
    : undefined,
})
