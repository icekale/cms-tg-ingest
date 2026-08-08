<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NDescriptions, NDescriptionsItem, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { displayTaskTitle } from '../taskView'

const message = useMessage()
const health = ref(null)
async function load() { try { health.value = await api.health() } catch (err) { message.error(err.message) } }
onMounted(load)
const guardTagType = (status) => ({ installed: 'success', missing: 'error', unknown: 'default', not_applicable: 'default' })[status] || 'default'
const guardLabel = (status) => ({ installed: '已安装', missing: '未安装', unknown: '状态未知', not_applicable: '不适用' })[status] || status
</script>

<template>
  <div class="page-title"><div><h1>本地健康</h1><p>TaskRunner、115 风控冷却和等待原因。</p></div><n-button secondary @click="load">刷新</n-button></div>
  <n-card v-if="health">
    <n-tag :type="health.runner_heartbeat_stale ? 'warning' : 'success'">{{ health.runner_heartbeat_stale ? '心跳过期' : '运行正常' }}</n-tag>
    <n-descriptions bordered :column="2" style="margin-top: 18px">
      <n-descriptions-item label="待执行">{{ health.pending_count }}</n-descriptions-item>
      <n-descriptions-item label="运行中">{{ health.running_count }}</n-descriptions-item>
      <n-descriptions-item label="需处理">{{ health.problem_count }}</n-descriptions-item>
      <n-descriptions-item label="锁等待">{{ health.lock_wait_count }}</n-descriptions-item>
      <n-descriptions-item label="115 冷却">{{ health.p115_cooldown_active ? '冷却中' : '未冷却' }}</n-descriptions-item>
      <n-descriptions-item label="Runner 当前">
        <span v-if="health.runner_active && health.runner_active_task_id">处理 #{{ health.runner_active_task_id }}（{{ health.runner_active_stage || '?' }}）</span>
        <span v-else-if="health.runner_last_claim_attempt_at">空闲</span>
        <span v-else>未启动</span>
      </n-descriptions-item>
      <n-descriptions-item v-if="health.cms_strm_guard" label="CMS STRM 守卫">
        <n-tag :type="guardTagType(health.cms_strm_guard.status)">{{ guardLabel(health.cms_strm_guard.status) }}</n-tag>
        <div class="subtle" style="margin-top: 4px">{{ health.cms_strm_guard.message }}</div>
      </n-descriptions-item>
    </n-descriptions>
    <div v-if="health.wait_details?.length" class="health-list"><h3>等待原因</h3><div v-for="detail in health.wait_details" :key="detail">{{ detail }}</div></div>
    <div v-if="health.latest_problem" class="health-list"><h3>最近问题</h3><RouterLink :to="`/tasks/${health.latest_problem.id}`">#{{ health.latest_problem.id }} {{ displayTaskTitle(health.latest_problem) }}</RouterLink><div>{{ health.latest_problem.error?.summary || health.latest_problem.why_slow || '' }}</div></div>
  </n-card>
  <n-card v-else>正在加载…</n-card>
</template>
