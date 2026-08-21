import { h } from 'vue'

function strokeIcon(children) {
  return () => h('svg', {
    xmlns: 'http://www.w3.org/2000/svg',
    width: '18',
    height: '18',
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '2',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
  }, children)
}

function p(d) {
  return h('path', { d })
}

function c(cx, cy, r) {
  return h('circle', { cx, cy, r })
}

function r(x, y, width, height, rx = 1) {
  return h('rect', { x, y, width, height, rx })
}

export const navIcons = {
  overview: strokeIcon([
    r(3, 3, 7, 7),
    r(14, 3, 7, 7),
    r(3, 14, 7, 7),
    r(14, 14, 7, 7),
  ]),
  emby: strokeIcon([
    c(12, 12, 9),
    p('M10 8.5 16 12 10 15.5Z'),
  ]),
  tasks: strokeIcon([
    p('M8 6h13'),
    p('M8 12h13'),
    p('M8 18h13'),
    p('M3 6h.01'),
    p('M3 12h.01'),
    p('M3 18h.01'),
  ]),
  quality: strokeIcon([
    p('M12 3 4.5 6.5v5.2c0 4.7 3.2 7.9 7.5 9.3 4.3-1.4 7.5-4.6 7.5-9.3V6.5Z'),
    p('M9 12l2 2 4-4'),
  ]),
  health: strokeIcon([
    p('M22 12h-4l-3 7-6-14-3 7H2'),
  ]),
  hdhive: strokeIcon([
    c(6, 18, 2),
    p('M4 11a9 9 0 0 1 9 9'),
    p('M4 4a16 16 0 0 1 16 16'),
  ]),
  logs: strokeIcon([
    r(4, 4, 16, 16, 2),
    p('M8 9h8'),
    p('M8 12h8'),
    p('M8 15h5'),
  ]),
  settings: strokeIcon([
    c(12, 12, 3),
    p('M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.3.6.9 1 1.5 1.1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z'),
  ]),
}
