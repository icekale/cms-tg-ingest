<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NGrid, NGridItem, NStatistic, NSpace, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const data = ref(null)
const error = ref('')
async function load() { try { data.value = await api.overview(); error.value = '' } catch (err) { error.value = err.message } }
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>运行概览</h1><p>把当前队列、风险和下一步操作放在一个页面。</p></div><n-button secondary @click="load">刷新</n-button></div>
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
