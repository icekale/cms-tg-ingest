<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NDescriptions, NDescriptionsItem, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import { api } from '../api'

const route = useRoute(); const message = useMessage(); const task = ref(null)
async function load() { try { task.value = await api.task(route.params.id) } catch (err) { message.error(err.message) } }
async function changeMode(mode) { try { task.value = (await api.setTaskMode(route.params.id, mode)).task; message.success('任务 STRM 模式已保存') } catch (err) { message.error(err.message); await load() } }
onMounted(load)
</script>

<template>
  <div v-if="task" class="page-title"><div><h1>{{ task.title }}</h1><p>#{{ task.id }} · {{ task.stage }}</p></div><n-tag>{{ task.status }}</n-tag></div>
  <n-card v-if="task" title="任务详情"><n-descriptions bordered :column="2"><n-descriptions-item label="STRM 模式"><n-select style="width: 130px" :value="task.strm_mode" :options="[{label: '共享 STRM', value: 'shared'}, {label: '直链 STRM', value: 'direct'}]" @update:value="changeMode" /></n-descriptions-item><n-descriptions-item label="分类">{{ task.category || '-' }}</n-descriptions-item><n-descriptions-item label="为什么慢">{{ task.why_slow || '-' }}</n-descriptions-item><n-descriptions-item label="TMDB">{{ task.tmdb_id || '-' }}</n-descriptions-item></n-descriptions><div style="margin-top: 18px"><n-button secondary @click="load">刷新</n-button></div></n-card>
  <n-card v-else>正在加载任务…</n-card>
</template>
