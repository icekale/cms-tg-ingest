import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildLogStreamUrl,
  createLogStreamController,
  parseLogEvent,
  prependLog,
  preservedScrollTop,
} from '../src/logView.js'

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
  class FakeSource {
    constructor(url, options) {
      this.url = url
      this.options = options
      this.listeners = new Map()
      this.closed = false
      sources.push(this)
    }
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    close() { this.closed = true }
    emit(name, payload) { this.listeners.get(name)?.({ data: JSON.stringify(payload) }) }
  }
  const snapshots = []
  const controller = createLogStreamController(
    { onSnapshot: (rows) => snapshots.push(rows) },
    (url, options) => new FakeSource(url, options),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  sources[0].emit('snapshot', { entries: [{ id: 1 }] })
  controller.connect({ filterType: 'all', lines: 5000, keyword: '' })

  assert.deepEqual(snapshots, [[{ id: 1 }]])
  assert.equal(sources[0].closed, true)
  assert.equal(sources[1].options.withCredentials, true)
  controller.close()
  assert.equal(sources[1].closed, true)
})
