<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NDescriptions, NDescriptionsItem, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const message = useMessage()
const health = ref(null)
async function load() { try { health.value = await api.health() } catch (err) { message.error(err.message) } }
onMounted(load)
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
    </n-descriptions>
    <div v-if="health.wait_details?.length" class="health-list"><h3>等待原因</h3><div v-for="detail in health.wait_details" :key="detail">{{ detail }}</div></div>
    <div v-if="health.latest_problem" class="health-list"><h3>最近问题</h3><RouterLink :to="`/tasks/${health.latest_problem.id}`">#{{ health.latest_problem.id }} {{ health.latest_problem.title }}</RouterLink><div>{{ health.latest_problem.error?.summary || health.latest_problem.why_slow || '' }}</div></div>
  </n-card>
  <n-card v-else>正在加载…</n-card>
</template>
