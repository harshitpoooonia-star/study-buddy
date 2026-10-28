// Shared helpers: API calls, IST formatting, small reactive state.

export const TZ = 'Asia/Kolkata'

export class AuthError extends Error {}

export async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', headers: { 'x-sb': '1' }, ...opts }
  if (opts.body && typeof opts.body !== 'string') {
    init.body = JSON.stringify(opts.body)
    init.headers = { ...init.headers, 'content-type': 'application/json' }
  }
  const r = await fetch(`/dash/api/${path}`, init)
  if (r.status === 401) throw new AuthError('login required')
  let data = null
  try { data = await r.json() } catch { /* empty */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${r.status}`)
  if (data && data.offline) throw new Error('offline')
  return data
}

const fmt = (o) => new Intl.DateTimeFormat('en-IN', { timeZone: TZ, ...o })
const fTime = fmt({ hour: 'numeric', minute: '2-digit', hour12: true })
const fDay = fmt({ weekday: 'short', day: 'numeric', month: 'short' })
const fDayLong = fmt({ weekday: 'long', day: 'numeric', month: 'long' })
const fYmd = fmt({ year: 'numeric', month: '2-digit', day: '2-digit' })

export const time = (ts) => fTime.format(ts * 1000).replace(' ', ' ').toLowerCase()
export const day = (ts) => fDay.format(ts * 1000)
export const dayLong = (ts) => fDayLong.format(ts * 1000)
export const ymd = (ts) => { const p = fYmd.formatToParts(ts * 1000); const g = (t) => p.find((x) => x.type === t).value; return `${g('year')}-${g('month')}-${g('day')}` }
export const dateOf = (s) => new Date(`${s}T00:00:00+05:30`).getTime() / 1000

export function hm(s) { // "08:20" -> "8:20"
  const [h, m] = s.split(':').map(Number)
  const ap = h >= 12 ? 'pm' : 'am'
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${ap}`
}

export function rel(ts, now = Date.now() / 1000) {
  const d = ts - now
  const a = Math.abs(d)
  let s
  if (a < 60) s = `${Math.round(a)}s`
  else if (a < 3600) s = `${Math.round(a / 60)}m`
  else if (a < 86400 * 2) { const h = Math.floor(a / 3600); const m = Math.round((a % 3600) / 60); s = m && h < 10 ? `${h}h ${m}m` : `${h}h` }
  else s = `${Math.round(a / 86400)}d`
  return d >= 0 ? `in ${s}` : `${s} ago`
}

export function countdown(ts, now) {
  let d = Math.max(0, Math.round(ts - now))
  const h = Math.floor(d / 3600); d %= 3600
  const m = Math.floor(d / 60); const s = d % 60
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}:${String(s).padStart(2, '0')}`
}

export function ago(ts) {
  if (!ts) return 'never'
  return rel(ts).replace(' ago', '') + ' ago'
}

export const STATUS = {
  submitted: ['Submitted', 'ok'], graded: ['Graded', 'ok'], attempted: ['Attempted', 'ok'], done: ['Done', 'ok'],
  draft: ['Draft', 'warn'], pending: ['Not submitted', 'warn'], open: ['Open', 'info'], upcoming: ['Opens later', 'muted'],
  closed: ['Closed', 'muted'], unknown: ['—', 'muted'], upcoming_exam: ['Exam', 'danger'],
}

export function statusOf(it) {
  if (it.mark === 'done' && !['submitted', 'graded', 'attempted'].includes(it.status)) return ['Marked done', 'ok']
  if (it.bucket === 'overdue' && !it.done && it.source !== 'exam') return ['Overdue', 'danger']
  if (it.source === 'exam') return it.done ? ['Done', 'muted'] : ['Exam', 'danger']
  if (it.source === 'manual') return it.done ? ['Done', 'ok'] : ['To do', 'info']
  return STATUS[it.status] || [it.status || '—', 'muted']
}

export const KIND = { assign: 'Assignment', quiz: 'Quiz', event: 'Event', task: 'Task', exam: 'Exam' }

// Short subject labels used across pages ("Automata Theory and Computability" -> "ATC").
export function short(name = '') {
  const stop = new Set(['and', 'of', 'for', 'the', 'using', 'in', 'to'])
  const w = name.split(/\s+/).filter((x) => /^[A-Za-z]/.test(x) && !stop.has(x.toLowerCase()))
  return (w.length > 1 ? w.map((x) => x[0].toUpperCase()).join('') : name).slice(0, 5)
}

export function toast(msg) {
  window.dispatchEvent(new CustomEvent('sb-toast', { detail: msg }))
}

// ---- v3 helpers ----------------------------------------------------------------------------------

export const esc = (s = '') => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

// Search snippets carry \u0001 … \u0002 around matches.
export const hl = (s = '') => esc(s).replace(/\u0001/g, '<mark>').replace(/\u0002/g, '</mark>')

export function hashQuery() {
  return new URLSearchParams(location.hash.split('?')[1] || '')
}

export function kb(n = 0) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
}

// Small, safe Markdown for AI answers: bold, italic, inline code, https links, bullets, line breaks.
export function md(src = '') {
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,:;!?]|$)/g, '$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
  const out = []
  let list = false
  for (const raw of String(src).split('\n')) {
    const line = raw.trimEnd()
    const b = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/)
    if (b) { if (!list) { out.push('<ul>'); list = true } out.push(`<li>${inline(b[1])}</li>`); continue }
    if (list) { out.push('</ul>'); list = false }
    const h = line.match(/^#{1,6}\s+(.*)/)
    if (h) out.push(`<p><b>${inline(h[1])}</b></p>`)
    else if (line.trim() === '' || /^\|?\s*:?-{2,}/.test(line)) continue
    else out.push(`<p>${inline(line)}</p>`)
  }
  if (list) out.push('</ul>')
  return out.join('')
}

export function debounce(fn, ms = 250) {
  let t
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms) }
}

// Optional link to an external dashboard. Set VITE_EXT_DASH_HOST and VITE_EXT_DASH_URL
// at build time to show it only on that host; leave them unset to hide the link.
const DASH_HOST = import.meta.env.VITE_EXT_DASH_HOST
export const EXT_DASH = DASH_HOST && location.host === DASH_HOST
  ? (import.meta.env.VITE_EXT_DASH_URL || null) : null
