<script setup>
import { computed, onMounted, ref } from 'vue'
import { NButton, NCard, NDescriptions, NDescriptionsItem, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import { api } from '../api'

const route = useRoute()
const message = useMessage()
const task = ref(null)
const busyAction = ref('')
const terminalStatuses = ['succeeded', 'failed', 'needs_action']
const downstreamStages = ['moved', 'emby_confirmed', 'cleaned']

async function load() {
  try { task.value = await api.task(route.params.id) } catch (err) { message.error(err.message) }
}

async function changeMode(mode) {
  try {
    task.value = (await api.setTaskMode(route.params.id, mode)).task
    message.success('任务 STRM 模式已保存')
  } catch (err) { message.error(err.message); await load() }
}

const canRetry = computed(() => task.value && task.value.status === 'failed' && !task.value.claimed)
const canDownstream = computed(() => task.value && downstreamStages.includes(task.value.stage) && terminalStatuses.includes(task.value.status) && !task.value.claimed)
const canReprocess = computed(() => task.value && terminalStatuses.includes(task.value.status) && !task.value.claimed)

async function runAction(action) {
  if (['restore', 'reprocess'].includes(action) && !window.confirm(action === 'restore' ? '确认恢复该任务的 STRM？' : '确认从头重跑该任务？')) return
  busyAction.value = action
  try {
    task.value = await api.taskAction(route.params.id, action)
    message.success('操作已入队')
  } catch (err) { message.error(err.message); await load() } finally { busyAction.value = '' }
}

function eventTime(value) { return value ? new Date(value * 1000).toLocaleString() : '-' }
onMounted(load)
</script>

<template>
  <div v-if="task" class="page-title"><div><h1>{{ task.title }}</h1><p>#{{ task.id }} · {{ task.stage }}</p></div><n-tag>{{ task.status }}</n-tag></div>
  <n-card v-if="task" title="任务详情">
    <n-descriptions bordered :column="2">
      <n-descriptions-item label="STRM 模式"><n-select style="width: 130px" :value="task.strm_mode" :options="[{label: '共享 STRM', value: 'shared'}, {label: '直链 STRM', value: 'direct'}]" @update:value="changeMode" /></n-descriptions-item>
      <n-descriptions-item label="分类">{{ task.category || '-' }}</n-descriptions-item>
      <n-descriptions-item label="为什么慢">{{ task.why_slow || '-' }}</n-descriptions-item>
      <n-descriptions-item label="阶段耗时">{{ task.stage_elapsed || '-' }}</n-descriptions-item>
      <n-descriptions-item label="115 调用">{{ task.stage_p115_calls || '-' }}</n-descriptions-item>
      <n-descriptions-item label="TMDB">{{ task.tmdb_id || '-' }}</n-descriptions-item>
    </n-descriptions>
    <n-space style="margin-top: 18px">
      <n-button v-if="canRetry" type="primary" :loading="busyAction === 'retry'" @click="runAction('retry')">重试 retry</n-button>
      <n-button v-if="canDownstream" :loading="busyAction === 'emby'" @click="runAction('emby')">查 Emby emby</n-button>
      <n-button v-if="canDownstream" :loading="busyAction === 'restore'" @click="runAction('restore')">恢复 STRM restore</n-button>
      <n-button v-if="canReprocess" type="warning" :loading="busyAction === 'reprocess'" @click="runAction('reprocess')">从头重跑 reprocess</n-button>
      <n-button secondary @click="load">刷新</n-button>
    </n-space>
    <n-card title="处理时间线" embedded style="margin-top: 18px">
      <div v-for="event in task.events || []" :key="event.id" class="event-row"><n-tag size="small">{{ event.stage }}</n-tag><span>{{ event.message }}</span><span class="muted">{{ eventTime(event.created_at) }}</span></div>
      <div v-if="!(task.events || []).length" class="muted">暂无事件</div>
    </n-card>
    <n-card title="错误与技术详情" embedded style="margin-top: 18px">
      <div v-if="task.error?.summary" class="error-text">{{ task.error.summary }}</div>
      <pre class="detail-text">{{ JSON.stringify(task.metadata || {}, null, 2) }}</pre>
    </n-card>
  </n-card>
  <n-card v-else>正在加载任务…</n-card>
</template>
