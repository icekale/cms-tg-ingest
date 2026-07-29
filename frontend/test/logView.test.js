import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildLogStreamUrl,
  createLogStreamController,
  parseLogEvent,
  prependLog,
  preservedScrollTop,
} from '../src/logView.js'

class FakeSource {
  constructor(url, options) {
    this.url = url
    this.options = options
    this.listeners = new Map()
    this.closed = false
  }
  addEventListener(name, callback) { this.listeners.set(name, callback) }
  close() { this.closed = true }
  emit(name, payload) { this.emitRaw(name, JSON.stringify(payload)) }
  emitRaw(name, data) { this.listeners.get(name)?.({ data }) }
  open() { this.onopen?.() }
  fail() { this.onerror?.() }
}

function createFakeSourceFactory(sources) {
  return (url, options) => {
    const source = new FakeSource(url, options)
    sources.push(source)
    return source
  }
}

test('buildLogStreamUrl sends only documented filters and never a web token', () => {
  const url = buildLogStreamUrl({ filterType: 'ERROR', lines: 2000, keyword: 'CMS 失败' })
  assert.equal(url, '/api/v1/logs/stream?filter_type=ERROR&lines=2000&keyword=CMS+%E5%A4%B1%E8%B4%A5')
  assert.equal(url.includes('token='), false)
})

test('log state keeps newest first, enforces limit, and parses multiline payloads', () => {
  const entry = parseLogEvent({ data: '{"id":3,"level":"ERROR","text":"line one\\nline two"}' })
  const rows = prependLog([{ id: 2 }, { id: 1 }], entry, 2)

  assert.equal(entry.text, 'line one\nline two')
  assert.deepEqual(rows.map((row) => row.id), [3, 2])
  assert.equal(preservedScrollTop(true, 120, 800, 860), 180)
  assert.equal(preservedScrollTop(false, 120, 800, 860), 0)
})

test('controller closes the previous EventSource on reconnect and on disposal', () => {
  const sources = []
  const snapshots = []
  const controller = createLogStreamController(
    { onSnapshot: (rows) => snapshots.push(rows) },
    createFakeSourceFactory(sources),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  sources[0].emit('snapshot', { entries: [{ id: 1, level: 'INFO', text: 'snapshot' }] })
  controller.connect({ filterType: 'all', lines: 5000, keyword: '' })

  assert.deepEqual(snapshots, [[{ id: 1, level: 'INFO', text: 'snapshot' }]])
  assert.equal(sources[0].closed, true)
  assert.equal(sources[1].options.withCredentials, true)
  controller.close()
  assert.equal(sources[1].closed, true)
})

test('controller ignores stale lifecycle, snapshot, and gap events after reconnect', () => {
  const sources = []
  const snapshots = []
  const gaps = []
  let opens = 0
  let errors = 0
  const controller = createLogStreamController(
    {
      onOpen: () => { opens += 1 },
      onError: () => { errors += 1 },
      onSnapshot: (rows) => snapshots.push(rows),
      onGap: (payload) => gaps.push(payload),
    },
    createFakeSourceFactory(sources),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  controller.connect({ filterType: 'all', lines: 5000, keyword: '' })

  sources[0].open()
  sources[0].fail()
  sources[1].open()
  sources[1].emit('snapshot', { entries: [{ id: 2, level: 'INFO', text: 'current' }] })
  sources[0].emit('snapshot', { entries: [{ id: 1, level: 'INFO', text: 'stale' }] })
  sources[0].emit('gap', { reason: 'slow_client' })

  assert.equal(opens, 1)
  assert.equal(errors, 0)
  assert.deepEqual(snapshots, [[{ id: 2, level: 'INFO', text: 'current' }]])
  assert.deepEqual(gaps, [])
})

test('parseLogEvent rejects an empty SSE payload', () => {
  assert.throws(() => parseLogEvent({ data: '' }), /日志事件格式无效/)
})

test('controller rejects a partial snapshot without replacing current rows', () => {
  const sources = []
  const snapshots = []
  let errors = 0
  const controller = createLogStreamController(
    {
      onError: () => { errors += 1 },
      onSnapshot: (rows) => snapshots.push(rows),
    },
    createFakeSourceFactory(sources),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  sources[0].emit('snapshot', { entries: [{ id: 1, level: 'INFO', text: 'kept' }] })

  assert.doesNotThrow(() => sources[0].emit('snapshot', { entries: [{ id: 2, level: 'INFO' }] }))
  assert.deepEqual(snapshots, [[{ id: 1, level: 'INFO', text: 'kept' }]])
  assert.equal(errors, 1)
})

test('controller isolates malformed frames and invalid payload shapes', () => {
  const sources = []
  const snapshots = []
  const logs = []
  const gaps = []
  let heartbeats = 0
  let errors = 0
  const controller = createLogStreamController(
    {
      onError: () => { errors += 1 },
      onSnapshot: (rows) => snapshots.push(rows),
      onLog: (entry) => logs.push(entry),
      onHeartbeat: () => { heartbeats += 1 },
      onGap: (payload) => gaps.push(payload),
    },
    createFakeSourceFactory(sources),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  sources[0].emit('snapshot', { entries: [{ id: 1, level: 'INFO', text: 'kept' }] })

  assert.doesNotThrow(() => sources[0].emitRaw('snapshot', ''))
  assert.doesNotThrow(() => sources[0].emitRaw('snapshot', '{'))
  assert.doesNotThrow(() => sources[0].emit('snapshot', { entries: {} }))
  assert.doesNotThrow(() => sources[0].emit('snapshot', { entries: [{ id: 'bad', level: 'INFO', text: 'bad' }] }))
  assert.doesNotThrow(() => sources[0].emit('log', { id: 'bad', level: 'INFO', text: 'bad' }))
  assert.doesNotThrow(() => sources[0].emit('heartbeat', { time: 'bad' }))
  assert.doesNotThrow(() => sources[0].emit('gap', { reason: 1 }))

  assert.deepEqual(snapshots, [[{ id: 1, level: 'INFO', text: 'kept' }]])
  assert.deepEqual(logs, [])
  assert.equal(heartbeats, 0)
  assert.deepEqual(gaps, [])
  assert.equal(errors, 7)
})
