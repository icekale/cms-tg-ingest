<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'

const message = useMessage()
const tasks = ref([])
const defaultMode = ref('shared')
const loading = ref(false)
const columns = [
  { title: '任务', key: 'title', render: (row) => h(RouterLink, { class: 'task-link', to: `/tasks/${row.id}` }, { default: () => `#${row.id} ${row.title}` }) },
  { title: '阶段', key: 'stage' },
  { title: '状态', key: 'status', render: (row) => h(NTag, { size: 'small' }, { default: () => row.status }) },
  { title: 'STRM', key: 'strm_mode', render: (row) => row.strm_mode === 'shared' ? '共享' : '直链' },
  { title: '为什么慢', key: 'why_slow' },
]
async function load() { loading.value = true; try { const [taskData, overview] = await Promise.all([api.tasks(), api.overview()]); tasks.value = taskData.items; defaultMode.value = overview.strm_default_mode } catch (err) { message.error(err.message) } finally { loading.value = false } }
async function changeDefault(value) { try { await api.setDefaultMode(value); defaultMode.value = value; message.success('默认模式已保存') } catch (err) { message.error(err.message) } }
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>当前任务</h1><p>任务级模式在 STRM 副作用开始前可调整，进入锁定阶段后不可更改。</p></div><n-space><n-select style="width: 130px" :value="defaultMode" :options="[{label: '共享 STRM', value: 'shared'}, {label: '直链 STRM', value: 'direct'}]" @update:value="changeDefault" /><n-button secondary :loading="loading" @click="load">刷新</n-button></n-space></div>
  <n-card><n-data-table :columns="columns" :data="tasks" :loading="loading" :pagination="{ pageSize: 20 }" /></n-card>
</template>
