<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'
import { NButton, NCard, NInput, NModal, NPopconfirm, NTag, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const SESSION_KEY = 'assistant_session_id'
const messages = ref([])
const input = ref('')
const busy = ref(false)
const sessionId = ref('')
const listEl = ref(null)
const quickPrompts = [
  '当前系统健康状态如何？',
  '最近有哪些任务失败或需要人工处理？',
  '讲解一下任务从接入到入库的完整流程',
]

onMounted(() => {
  try { sessionId.value = localStorage.getItem(SESSION_KEY) || '' } catch (_) { /* 隐私模式下忽略 */ }
})

watch(() => messages.value.length, scrollToBottom)

function scrollToBottom() {
  nextTick(() => {
    const el = listEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function send(text) {
  const question = String(text ?? input.value ?? '').trim()
  if (!question || busy.value) return
  messages.value.push({ role: 'user', content: question })
  input.value = ''
  busy.value = true
  scrollToBottom()
  try {
    const payload = { question }
    if (sessionId.value) payload.session_id = sessionId.value
    const data = await api.assistantChat(payload)
    sessionId.value = data.session_id || ''
    try { if (sessionId.value) localStorage.setItem(SESSION_KEY, sessionId.value) } catch (_) { /* 同上 */ }
    messages.value.push({ role: 'assistant', content: data.reply })
  } catch (err) {
    message.error(err.message)
  } finally {
    busy.value = false
    scrollToBottom()
  }
}

function resetChat() {
  messages.value = []
  sessionId.value = ''
  try { localStorage.removeItem(SESSION_KEY) } catch (_) { /* 同上 */ }
}

const memory = ref({ show: false, entries: [] })

async function openMemory() {
  memory.value.show = true
  await reloadMemory()
}

async function reloadMemory() {
  try {
    const data = await api.assistantMemory()
    memory.value.entries = data.entries || []
  } catch (err) {
    message.error(err.message)
  }
}

async function removeMemory(entry) {
  try {
    const data = await api.assistantMemoryDelete([entry.index])
    memory.value.entries = data.entries || []
    message.success('已删除')
  } catch (err) {
    message.error(err.message)
  }
}
</script>

<template>
  <div class="page-title">
    <div>
      <h1>AI 助手</h1>
      <p>基于 pi 的运维诊断：回答前会自动附上当前健康状态与任务快照，只给建议不执行操作。</p>
    </div>
    <div class="page-actions">
      <n-button secondary :disabled="busy" @click="openMemory">记忆</n-button>
      <n-button secondary :disabled="busy" @click="resetChat">新对话</n-button>
    </div>
  </div>
  <n-card content-style="display: flex; flex-direction: column; gap: 12px;">
    <div ref="listEl" class="assistant-list">
      <div v-if="!messages.length" class="assistant-empty subtle">
        <p>试着问：</p>
        <n-tag v-for="prompt in quickPrompts" :key="prompt" size="small" :bordered="false" style="cursor: pointer" @click="send(prompt)">{{ prompt }}</n-tag>
      </div>
      <div v-for="(item, index) in messages" :key="index" class="assistant-msg" :class="item.role">{{ item.content }}</div>
      <div v-if="busy" class="assistant-msg assistant subtle">正在分析系统快照…</div>
    </div>
    <div class="assistant-input">
      <n-input
        v-model:value="input"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 5 }"
        placeholder="描述问题，例如：#12 任务为什么一直 needs_action？"
        :disabled="busy"
        @keydown.enter.exact.prevent="send()"
      />
      <n-button type="primary" :loading="busy" @click="send()">发送</n-button>
    </div>
  </n-card>
  <n-modal v-model:show="memory.show" preset="card" title="AI 长期记忆" class="memory-modal">
    <p class="subtle">助手从对话里自己记下的偏好与教训，每轮对话都会带上；删掉就不再使用。</p>
    <div v-if="!memory.entries.length" class="subtle" style="margin-top: 12px">还没有记忆。</div>
    <div v-for="entry in memory.entries" :key="entry.index" class="memory-row">
      <span>{{ entry.index }}. {{ entry.text }}</span>
      <n-popconfirm @positive-click="removeMemory(entry)">
        <template #trigger>
          <n-button size="tiny" quaternary>删除</n-button>
        </template>
        删除这条记忆？
      </n-popconfirm>
    </div>
  </n-modal>
</template>

<style scoped>
.assistant-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: calc(100vh - 320px);
  min-height: 240px;
  overflow-y: auto;
}
.assistant-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.assistant-msg {
  max-width: 82%;
  padding: 8px 12px;
  border-radius: 10px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
}
.assistant-msg.user {
  align-self: flex-end;
  background: var(--primary);
  color: var(--primary-invert);
}
.assistant-msg.assistant {
  align-self: flex-start;
  background: var(--primary-soft);
}
.assistant-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.assistant-input .n-button {
  flex-shrink: 0;
}
.memory-modal {
  max-width: 720px;
  width: 90%;
}
.memory-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border-soft, rgba(128, 128, 128, 0.18));
  line-height: 1.6;
  word-break: break-word;
}
</style>
