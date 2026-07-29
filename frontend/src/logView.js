export function buildLogStreamUrl({ filterType = 'main', lines = 1000, keyword = '' } = {}) {
  const params = new URLSearchParams({
    filter_type: filterType,
    lines: String(lines),
    keyword,
  })
  return `/api/v1/logs/stream?${params.toString()}`
}

export function parseLogEvent(event) {
  if (typeof event?.data !== 'string' || !event.data.trim()) throw new Error('日志事件格式无效')
  let payload
  try {
    payload = JSON.parse(event.data)
  } catch (_) {
    throw new Error('日志事件格式无效')
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('日志事件格式无效')
  return payload
}

export function prependLog(entries, entry, limit) {
  return [entry, ...entries.filter((item) => item.id !== entry.id)].slice(0, Number(limit) || 1000)
}

export function preservedScrollTop(readingOlder, previousTop, previousHeight, nextHeight) {
  return readingOlder ? previousTop + Math.max(0, nextHeight - previousHeight) : 0
}

function parseLogEntry(payload) {
  if (!Number.isInteger(payload.id) || typeof payload.level !== 'string' || typeof payload.text !== 'string') {
    throw new Error('日志事件格式无效')
  }
  return payload
}

function parseSnapshot(event) {
  const payload = parseLogEvent(event)
  if (!Array.isArray(payload.entries)) throw new Error('日志事件格式无效')
  return payload.entries.map(parseLogEntry)
}

function parseHeartbeat(event) {
  const payload = parseLogEvent(event)
  if (!Number.isFinite(payload.time)) throw new Error('日志事件格式无效')
  return payload
}

function parseGap(event) {
  const payload = parseLogEvent(event)
  if (typeof payload.reason !== 'string' || !payload.reason) throw new Error('日志事件格式无效')
  return payload
}

export function createLogStreamController(callbacks = {}, sourceFactory) {
  const factory = sourceFactory || ((url, options) => new EventSource(url, options))
  let source = null

  function close() {
    const current = source
    source = null
    if (current) current.close()
  }

  function addCurrentListener(current, name, parse, callback) {
    current.addEventListener(name, (event) => {
      if (source !== current) return
      let payload
      try {
        payload = parse(event)
      } catch (_) {
        callbacks.onError?.()
        return
      }
      callback?.(payload)
    })
  }

  function connect(filters) {
    close()
    const current = factory(buildLogStreamUrl(filters), { withCredentials: true })
    source = current
    current.onopen = () => {
      if (source === current) callbacks.onOpen?.()
    }
    current.onerror = () => {
      if (source === current) callbacks.onError?.()
    }
    // Invalid current-source frames report through onError without reaching view callbacks.
    addCurrentListener(current, 'snapshot', parseSnapshot, callbacks.onSnapshot)
    addCurrentListener(current, 'log', (event) => parseLogEntry(parseLogEvent(event)), callbacks.onLog)
    addCurrentListener(current, 'heartbeat', parseHeartbeat, callbacks.onHeartbeat)
    addCurrentListener(current, 'gap', parseGap, callbacks.onGap)
    return current
  }

  return { connect, close }
}
