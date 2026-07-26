<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NSpace, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const message = useMessage()
const tasks = ref([])
const loading = ref(false)
const modeLabels = { shared: '自有分享', direct: '直链', source_shared: '原始分享' }
const columns = [
  { title: '任务', key: 'title', render: (row) => h(RouterLink, { class: 'task-link', to: `/tasks/${row.id}` }, { default: () => `#${row.id} ${row.title}` }) },
  { title: '阶段', key: 'stage' },
  { title: '状态', key: 'status', render: (row) => h(NTag, { size: 'small' }, { default: () => row.status }) },
  { title: 'STRM', key: 'strm_mode', render: (row) => modeLabels[row.strm_mode] || row.strm_mode },
  { title: '为什么慢', key: 'why_slow' },
]
async function load() { loading.value = true; try { tasks.value = (await api.tasks()).items } catch (err) { message.error(err.message) } finally { loading.value = false } }
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>当前任务</h1><p>任务级模式在 STRM 副作用开始前可调整，进入锁定阶段后不可更改。</p></div><n-space><n-button secondary :loading="loading" @click="load">刷新</n-button></n-space></div>
  <n-card><n-data-table :columns="columns" :data="tasks" :loading="loading" :pagination="{ pageSize: 20 }" /></n-card>
</template>
