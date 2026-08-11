<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  NConfigProvider,
  NLayout,
  NLayoutContent,
  NLayoutFooter,
  NLayoutHeader,
  NLayoutSider,
  NMenu,
  NMessageProvider,
  darkTheme,
} from 'naive-ui'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { useTheme } from './useTheme'
import { darkThemeOverrides, lightThemeOverrides } from './themeOverrides'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const program = ref({ app_name: 'cms-tg-ingest', version: '' })
const brandLogoUrl = `${import.meta.env.BASE_URL}brand/logo-mark.svg`
const { mode, isDark, toggle } = useTheme()
const menuOptions = [
  { label: '运行概览', key: '/overview' },
  { label: 'Emby 看板', key: '/emby-board' },
  { label: '当前任务', key: '/tasks' },
  { label: '质量巡检', key: '/quality' },
  { label: '本地健康', key: '/health' },
  { label: 'HDHive 订阅', key: '/hdhive' },
  { label: '实时日志', key: '/logs' },
  { label: '设置', key: '/settings' },
]
const activeKey = computed(() => route.path.startsWith('/tasks/') ? '/tasks' : route.path)
const themeTitle = computed(() => {
  if (mode.value === 'system') {
    return isDark.value ? '跟随系统（暗色）· 点击切换为亮色' : '跟随系统（亮色）· 点击切换为暗色'
  }
  return isDark.value ? '暗色 · 点击恢复跟随系统' : '亮色 · 点击恢复跟随系统'
})
function navigate(key) { router.push(key) }
onMounted(async () => {
  try { program.value = await api.settings() } catch (_) { /* Footer stays useful while the API is unavailable. */ }
})
</script>

<template>
  <n-config-provider :theme="isDark ? darkTheme : null" :theme-overrides="isDark ? darkThemeOverrides : lightThemeOverrides">
    <n-message-provider>
      <n-layout class="admin-shell">
      <n-layout-header bordered class="top-header">
        <div class="brand"><img class="brand-logo" :src="brandLogoUrl" alt="" width="38" height="38" /><span>入库助手</span></div>
        <div class="header-actions">
          <div class="header-note">115 · CMS · Emby 工作流</div>
          <button type="button" class="theme-toggle" :title="themeTitle" :aria-label="themeTitle" @click="toggle">
            <svg v-if="isDark" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" /></svg>
          </button>
        </div>
      </n-layout-header>
      <n-layout has-sider>
        <n-layout-sider bordered collapse-mode="width" :collapsed-width="64" :width="220" :collapsed="collapsed" show-trigger @collapse="collapsed = true" @expand="collapsed = false">
          <n-menu :value="activeKey" :options="menuOptions" @update:value="navigate" />
        </n-layout-sider>
        <n-layout class="main-column">
          <n-layout-content class="content-wrap">
            <div class="content-inner"><router-view /></div>
          </n-layout-content>
          <n-layout-footer class="app-footer">
            <div class="footer-signature">
              <div class="footer-identity">
                <img class="footer-logo" :src="brandLogoUrl" alt="" width="24" height="24" />
                <span>
                  <span class="footer-product">入库助手</span>
                  <span class="footer-caption">115 · CMS · Emby 工作流</span>
                </span>
              </div>
              <span v-if="program.version" class="footer-version">v{{ program.version }}</span>
            </div>
          </n-layout-footer>
        </n-layout>
      </n-layout>
      </n-layout>
    </n-message-provider>
  </n-config-provider>
</template>
