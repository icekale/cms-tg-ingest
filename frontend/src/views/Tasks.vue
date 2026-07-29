<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NCard, NDataTable, NPopconfirm, NSpace, NTag, useMessage } from 'naive-ui'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { taskLifecycleState, taskStatusLabel } from '../taskView'

const message = useMessage()
const tasks = ref([])
const loading = ref(false)
const busyActions = ref({})
const modeLabels = { shared: '自有分享', direct: '直链', source_shared: '原始分享' }
const actionKey = (row, action) => `${row.id}:${action}`
const isActionBusy = (row, action) => Boolean(busyActions.value[actionKey(row, action)])
function setActionBusy(row, action, value) {
  const next = { ...busyActions.value }
  if (value) next[actionKey(row, action)] = true
  else delete next[actionKey(row, action)]
  busyActions.value = next
}
const columns = [
  { title: '任务', key: 'title', render: (row) => h(RouterLink, { class: 'task-link', to: `/tasks/${row.id}` }, { default: () => `#${row.id} ${row.title}` }) },
  { title: '阶段', key: 'stage' },
  { title: '状态', key: 'status', render: (row) => h(NTag, { size: 'small' }, { default: () => taskStatusLabel(row.status) }) },
  { title: 'STRM', key: 'strm_mode', render: (row) => modeLabels[row.strm_mode] || row.strm_mode },
  { title: '为什么慢', key: 'why_slow' },
  {
    title: '操作',
    key: 'actions',
    render: (row) => {
      const { canTerminate, canDelete, terminationRequested } = taskLifecycleState(row)
      const actions = []
      if (canTerminate) actions.push(h(NPopconfirm, { onPositiveClick: () => runLifecycleAction(row, 'terminate') }, {
        trigger: () => h(NButton, { type: 'warning', size: 'small', loading: isActionBusy(row, 'terminate') }, { default: () => '终止' }),
        default: () => '终止只会阻止后续阶段，当前已发出的 CMS/115 请求可能仍会完成。确认终止？',
      }))
      if (canDelete) actions.push(h(NPopconfirm, { onPositiveClick: () => runLifecycleAction(row, 'delete') }, {
        trigger: () => h(NButton, { type: 'error', ghost: true, size: 'small', loading: isActionBusy(row, 'delete') }, { default: () => '删除' }),
        default: () => '将永久删除本地任务、时间线和操作记录，不会删除网盘或媒体内容。确认删除？',
      }))
      if (terminationRequested) actions.push(h(NTag, { type: 'warning', size: 'small' }, { default: () => '终止处理中' }))
      return actions.length ? h(NSpace, { size: 'small' }, { default: () => actions }) : h('span', { class: 'muted' }, '-')
    },
  },
]
async function load() { loading.value = true; try { tasks.value = (await api.tasks()).items } catch (err) { message.error(err.message) } finally { loading.value = false } }
async function runLifecycleAction(row, action) {
  setActionBusy(row, action, true)
  try {
    if (action === 'terminate') {
      await api.taskAction(row.id, 'terminate')
      message.success('终止请求已提交')
    } else {
      await api.deleteTask(row.id)
      message.success('任务已删除')
    }
  } catch (err) {
    message.error(err.message)
  } finally {
    await load()
    setActionBusy(row, action, false)
  }
}
onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>当前任务</h1><p>任务级模式在 STRM 副作用开始前可调整，进入锁定阶段后不可更改。</p></div><n-space><n-button secondary :loading="loading" @click="load">刷新</n-button></n-space></div>
  <n-card><n-data-table :columns="columns" :data="tasks" :loading="loading" :pagination="{ pageSize: 20 }" /></n-card>
</template>
