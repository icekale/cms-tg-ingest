<script setup>
import { computed, onMounted, ref } from 'vue'
import { NLayoutFooter } from 'naive-ui'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { api } from './api'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const program = ref({ app_name: 'cms-tg-ingest', version: '' })
const brandLogoUrl = `${import.meta.env.BASE_URL}brand/logo-mark.svg`
const menuOptions = [
  { label: '运行概览', key: '/overview' },
  { label: '当前任务', key: '/tasks' },
  { label: '质量巡检', key: '/quality' },
  { label: '本地健康', key: '/health' },
  { label: 'HDHive 订阅', key: '/hdhive' },
  { label: '实时日志', key: '/logs' },
  { label: '设置', key: '/settings' },
]
const activeKey = computed(() => route.path.startsWith('/tasks/') ? '/tasks' : route.path)
function navigate(key) { router.push(key) }
onMounted(async () => {
  try { program.value = await api.settings() } catch (_) { /* Footer stays useful while the API is unavailable. */ }
})
</script>

<template>
  <n-config-provider>
    <n-message-provider>
      <n-layout class="admin-shell">
      <n-layout-header bordered class="top-header">
        <div class="brand"><img class="brand-logo" :src="brandLogoUrl" alt="" width="38" height="38" /><span>入库助手</span></div>
        <div class="header-note">115 · CMS · Emby 工作流</div>
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
