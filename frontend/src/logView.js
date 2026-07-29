export function buildLogStreamUrl({ filterType = 'main', lines = 1000, keyword = '' } = {}) {
  const params = new URLSearchParams({
    filter_type: filterType,
    lines: String(lines),
    keyword,
  })
  return `/api/v1/logs/stream?${params.toString()}`
}

export function parseLogEvent(event) {
  const payload = JSON.parse(event.data || '{}')
  if (!payload || typeof payload !== 'object') throw new Error('日志事件格式无效')
  return payload
}

export function prependLog(entries, entry, limit) {
  return [entry, ...entries.filter((item) => item.id !== entry.id)].slice(0, Number(limit) || 1000)
}

export function preservedScrollTop(readingOlder, previousTop, previousHeight, nextHeight) {
  return readingOlder ? previousTop + Math.max(0, nextHeight - previousHeight) : 0
}

export function createLogStreamController(callbacks = {}, sourceFactory) {
  const factory = sourceFactory || ((url, options) => new EventSource(url, options))
  let source = null

  function close() {
    if (source) source.close()
    source = null
  }

  function connect(filters) {
    close()
    const current = factory(buildLogStreamUrl(filters), { withCredentials: true })
    source = current
    current.onopen = () => callbacks.onOpen?.()
    current.onerror = () => callbacks.onError?.()
    current.addEventListener('snapshot', (event) => {
      const payload = parseLogEvent(event)
      callbacks.onSnapshot?.(Array.isArray(payload.entries) ? payload.entries : [])
    })
    current.addEventListener('log', (event) => callbacks.onLog?.(parseLogEvent(event)))
    current.addEventListener('heartbeat', (event) => callbacks.onHeartbeat?.(parseLogEvent(event)))
    current.addEventListener('gap', (event) => callbacks.onGap?.(parseLogEvent(event)))
    return current
  }

  return { connect, close }
}
