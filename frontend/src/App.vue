<script setup>
import { computed, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const menuOptions = [
  { label: '运行概览', key: '/overview' },
  { label: '当前任务', key: '/tasks' },
  { label: '质量巡检', key: '/quality' },
  { label: '本地健康', key: '/health' },
  { label: 'HDHive 订阅', key: '/hdhive' },
]
const activeKey = computed(() => route.path.startsWith('/tasks/') ? '/tasks' : route.path)
function navigate(key) { router.push(key) }
</script>

<template>
  <n-config-provider>
    <n-message-provider>
      <n-layout class="admin-shell">
      <n-layout-header bordered class="top-header">
        <div class="brand"><span class="brand-mark">CMS</span><span>入库助手</span></div>
        <div class="header-note">115 · CMS · Emby 工作流</div>
      </n-layout-header>
      <n-layout has-sider>
        <n-layout-sider bordered collapse-mode="width" :collapsed-width="64" :width="220" :collapsed="collapsed" show-trigger @collapse="collapsed = true" @expand="collapsed = false">
          <n-menu :value="activeKey" :options="menuOptions" @update:value="navigate" />
        </n-layout-sider>
        <n-layout-content class="content-wrap">
          <div class="content-inner"><router-view /></div>
        </n-layout-content>
      </n-layout>
      </n-layout>
    </n-message-provider>
  </n-config-provider>
</template>
