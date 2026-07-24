<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NSpace, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const issues = ref([])
const automation = ref(null)
const loading = ref(false)
const settings = ref({ enabled: false, time: '02:50', timezone: 'Asia/Shanghai', max_tasks: 50, check_limit: 3 })

async function load() {
  try {
    const data = await api.quality()
    issues.value = data.items || []
    automation.value = data.automation || null
    if (automation.value) settings.value = { ...settings.value, ...automation.value }
  } catch (err) { message.error(err.message) }
}

async function fix() {
  loading.value = true
  try { const result = await api.qualityFix(); message.success(`已入队 ${result.fixed || 0} 个修复任务`); await load() } catch (err) { message.error(err.message) } finally { loading.value = false }
}

async function run() {
  try { await api.qualityRun(); message.success('巡检已启动'); await load() } catch (err) { message.error(err.message) }
}

async function saveSettings() {
  try { await api.qualitySettings(settings.value); message.success('巡检设置已保存'); await load() } catch (err) { message.error(err.message) }
}

async function reset() {
  try { await api.qualityReset(); message.success('已恢复默认设置'); await load() } catch (err) { message.error(err.message) }
}

onMounted(load)
const columns = [{ title: '任务', key: 'title' }, { title: '问题', key: 'message' }, { title: '详情', key: 'detail' }]
</script>

<template>
  <div class="page-title"><div><h1>质量巡检</h1><p>检查本地 STRM 异常，并将安全修复重新加入任务队列。</p></div><n-space><n-button type="primary" :loading="loading" @click="fix">修复 fix</n-button><n-button secondary @click="run">立即巡检 run</n-button><n-button secondary @click="load">刷新</n-button></n-space></div>
  <n-card v-if="automation" title="自动巡检设置" class="section-card">
    <n-space align="center"><label>启用 <input v-model="settings.enabled" type="checkbox"></label><label>时间 <input v-model="settings.time" size="5"></label><label>时区 <input v-model="settings.timezone" size="18"></label><label>任务上限 <input v-model.number="settings.max_tasks" type="number" min="1"></label><label>115检查上限 <input v-model.number="settings.check_limit" type="number" min="1"></label><n-button @click="saveSettings">保存 settings</n-button><n-button secondary @click="reset">恢复 reset</n-button></n-space>
    <p class="muted">状态：{{ automation.status }}，下次运行：{{ automation.next_run_at }}</p>
  </n-card>
  <n-card><n-data-table :columns="columns" :data="issues" :pagination="{ pageSize: 20 }" /></n-card>
</template>
