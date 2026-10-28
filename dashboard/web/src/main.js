import './app.css'
import { mount } from 'svelte'
import App from './App.svelte'

try {
  const t = localStorage.getItem('sb-theme')
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t
} catch { /* storage blocked */ }

if ('serviceWorker' in navigator && location.protocol === 'https:') {
  navigator.serviceWorker.register('/dash/sw.js', { scope: '/dash/' }).catch(() => {})
}

const syncThemeColor = () => {
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
  document.querySelectorAll('meta[name=theme-color]').forEach((m) => (m.content = bg || m.content))
}
syncThemeColor()
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', syncThemeColor)
addEventListener('sb-theme', syncThemeColor)

export default mount(App, { target: document.getElementById('app') })
