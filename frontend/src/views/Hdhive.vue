<script setup>
import { onMounted, ref } from 'vue'
import { NCard, NDataTable, useMessage } from 'naive-ui'
import { api } from '../api'
const message = useMessage(); const subscriptions = ref([])
onMounted(async () => { try { subscriptions.value = (await api.hdhive()).subscriptions || [] } catch (err) { message.error(err.message) } })
const columns = [{ title: '订阅', key: 'title' }, { title: 'TMDB', key: 'tmdb_id' }, { title: '状态', key: 'status' }, { title: '资源数', key: 'items', render: (row) => row.items?.length || 0 }]
</script>
<template><div class="page-title"><div><h1>HDHive 订阅</h1><p>管理和查看订阅资源，解锁仍由 Telegram/CMS 工作流执行。</p></div></div><n-card><n-data-table :columns="columns" :data="subscriptions" :pagination="{ pageSize: 20 }" /></n-card></template>
