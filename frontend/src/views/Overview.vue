<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NPopconfirm, NSpace, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const data = ref(null)
const error = ref('')
const message = useMessage()
async function load() { try { data.value = await api.overview(); error.value = '' } catch (err) { error.value = err.message } }
async function clearHistory() {
  try {
    const result = await api.clearHistory()
    message.success(`已清理 ${result.cleared || 0} 条历史记录`)
    await load()
  } catch (err) { message.error(err.message) }
}
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>运行概览</h1><p>把当前队列、风险和下一步操作放在一个页面。</p></div><n-space><n-popconfirm @positive-click="clearHistory"><template #trigger><n-button secondary>清理历史</n-button></template>确认清理已完成历史任务？</n-popconfirm><n-button secondary @click="load">刷新</n-button></n-space></div>
  <n-card v-if="error" type="error" class="section-card">{{ error }}</n-card>
  <template v-else-if="data">
    <div class="metric-grid">
      <n-card><n-statistic label="活跃任务" :value="data.health.pending_count + data.health.running_count" /></n-card>
      <n-card><n-statistic label="需处理" :value="data.health.problem_count" /></n-card>
      <n-card><n-statistic label="锁等待" :value="data.health.lock_wait_count" /></n-card>
      <n-card><n-statistic label="默认 STRM" :value="data.strm_default_mode === 'shared' ? '共享' : '直链'" /></n-card>
    </div>
    <n-card title="当前队列" class="section-card">
      <n-space vertical>
        <div v-for="task in data.tasks.items.slice(0, 8)" :key="task.id">
          <router-link class="task-link" :to="`/tasks/${task.id}`">#{{ task.id }} {{ task.title }}</router-link>
          <span class="muted"> · {{ task.stage }} · </span><n-tag size="small">{{ task.status }}</n-tag>
        </div>
        <span v-if="!data.tasks.items.length" class="muted">暂无任务</span>
      </n-space>
    </n-card>
  </template>
</template>
