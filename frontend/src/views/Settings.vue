<script setup>
import { onMounted, ref } from 'vue'
import { NButton, NCard, NInput, NSelect, NSpace, NText, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const loading = ref(false)
const saving = ref(false)
const settings = ref(null)
const mode = ref('shared')
const receiveCode = ref('')

async function load() {
  loading.value = true
  try {
    settings.value = await api.settings()
    mode.value = settings.value.strm_default_mode
  } catch (err) { message.error(err.message) } finally { loading.value = false }
}

async function saveMode(value) {
  saving.value = true
  try {
    await api.setDefaultMode(value)
    mode.value = value
    settings.value.strm_default_mode = value
    message.success('默认 STRM 模式已保存')
  } catch (err) { message.error(err.message) } finally { saving.value = false }
}

async function saveReceiveCode() {
  try {
    const result = await api.setOwnShareReceiveCode(receiveCode.value)
    settings.value.own_share_receive_code = result.own_share_receive_code
    receiveCode.value = ''
    message.success('自有分享访问码已保存')
  } catch (err) { message.error(err.message) }
}

async function clearReceiveCode() {
  try {
    const result = await api.clearOwnShareReceiveCode()
    settings.value.own_share_receive_code = result.own_share_receive_code
    receiveCode.value = ''
    message.success('已恢复 CMS 或环境配置')
  } catch (err) { message.error(err.message) }
}

onMounted(load)
</script>

<template>
  <div class="page-title"><div><h1>设置</h1><p>统一管理新任务的默认 STRM 工作方式。</p></div><n-button secondary :loading="loading" @click="load">刷新</n-button></div>
  <n-card v-if="settings" title="STRM 模式" class="section-card">
    <n-space vertical :size="16">
      <n-select style="max-width: 320px" :value="mode" :options="settings.strm_modes" :loading="saving" @update:value="saveMode" />
      <n-text depth="3" v-if="mode === 'shared'">接收并由 CMS 整理，创建自己的永久分享后生成 STRM；Emby 入库确认后按配置清理源文件。</n-text>
      <n-text depth="3" v-if="mode === 'direct'">由 CMS 普通同步直接生成直链 STRM，不创建 115 分享。</n-text>
      <n-text depth="3" v-if="mode === 'source_shared'">直接使用收到的第三方 115 分享生成 STRM，跳过转存、整理、自有分享和 115 源文件清理。</n-text>
    </n-space>
  </n-card>
  <n-card v-if="settings" title="自有分享访问码" class="section-card">
    <n-space vertical :size="12">
      <n-text depth="3">当前：{{ settings.own_share_receive_code.masked }}（来源：{{ settings.own_share_receive_code.source }}）</n-text>
      <n-space>
        <n-input v-model:value="receiveCode" type="password" show-password-on="click" placeholder="例如 1212" style="width: 180px" />
        <n-button type="primary" :disabled="!receiveCode" @click="saveReceiveCode">保存</n-button>
        <n-button secondary @click="clearReceiveCode">使用 CMS 配置</n-button>
      </n-space>
    </n-space>
  </n-card>
</template>
