<script setup>
import { onMounted, ref } from 'vue'
import { NCard, NDescriptions, NDescriptionsItem, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
const message = useMessage(); const health = ref(null)
onMounted(async () => { try { health.value = await api.health() } catch (err) { message.error(err.message) } })
</script>
<template><div class="page-title"><div><h1>本地健康</h1><p>TaskRunner、115 风控冷却和等待原因。</p></div></div><n-card v-if="health"><n-tag :type="health.runner_heartbeat_stale ? 'warning' : 'success'">{{ health.runner_heartbeat_stale ? '心跳过期' : '运行正常' }}</n-tag><n-descriptions bordered :column="2" style="margin-top: 18px"><n-descriptions-item label="待执行">{{ health.pending_count }}</n-descriptions-item><n-descriptions-item label="运行中">{{ health.running_count }}</n-descriptions-item><n-descriptions-item label="需处理">{{ health.problem_count }}</n-descriptions-item><n-descriptions-item label="115 冷却">{{ health.p115_cooldown_active ? '冷却中' : '未冷却' }}</n-descriptions-item></n-descriptions></n-card><n-card v-else>正在加载…</n-card></template>
