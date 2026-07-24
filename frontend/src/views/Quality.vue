<script setup>
import { onMounted, ref } from 'vue'
import { NCard, NDataTable, useMessage } from 'naive-ui'
import { api } from '../api'
const message = useMessage(); const issues = ref([])
onMounted(async () => { try { issues.value = (await api.quality()).items || [] } catch (err) { message.error(err.message) } })
const columns = [{ title: '任务', key: 'title' }, { title: '问题', key: 'message' }, { title: '详情', key: 'detail' }]
</script>
<template><div class="page-title"><div><h1>质量巡检</h1><p>只读展示本地 STRM 异常，修复仍使用原有 Python 页面和按钮。</p></div></div><n-card><n-data-table :columns="columns" :data="issues" :pagination="{ pageSize: 20 }" /></n-card></template>
