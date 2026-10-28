<script>
  import { api, ago, toast } from './lib.js'
  import { I } from './icons.js'
  import BotSettings from './BotSettings.svelte'

  let { title, sub = '', sync = null, keys = null, children } = $props()

  let syncing = $state(false)
  let menu = $state(false)
  let botSheet = $state(false)
  let mode = $state(document.documentElement.dataset.theme || 'system')
  const NEXT = { system: 'light', light: 'dark', dark: 'system' }
  const LABEL = { system: 'Appearance · Auto', light: 'Appearance · Light', dark: 'Appearance · Dark' }
  let canInstall = $state(!!window.__sbInstall)

  addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); window.__sbInstall = e; canInstall = true })

  // Oldest successful sync across sources = how fresh the page really is.
  const freshest = $derived.by(() => {
    if (!sync) return null
    const src = (keys || ['moodle', 'attendance', 'timetable']).map((k) => sync[k]).filter(Boolean)
    const oks = src.map((s) => s.last_ok).filter(Boolean)
    const failing = src.some((s) => s.ok === false)
    return { ts: oks.length ? Math.min(...oks) : null, failing }
  })
  const stale = $derived(freshest && (!freshest.ts || Date.now() / 1000 - freshest.ts > 3 * 3600))

  async function refresh() {
    if (syncing) return
    syncing = true
    const before = sync?.last_run || 0
    try {
      await api('sync', { method: 'POST' })
      toast('Syncing with LMS and MyCamu…')
      for (let i = 0; i < 36; i++) {
        await new Promise((r) => setTimeout(r, 5000))
        const h = await api('health')
        if ((h.sync.last_run || 0) > before) {
          dispatchEvent(new CustomEvent('sb-refresh'))
          const bad = ['moodle', 'attendance', 'timetable', 'camu_unread'].filter((k) => h.sync[k]?.ok === false)
          toast(bad.length ? `Synced, but ${bad.join(', ')} failed` : 'Up to date')
          break
        }
      }
    } catch (e) { toast(e.message) }
    syncing = false
  }

  function theme() {
    mode = NEXT[mode]
    if (mode === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = mode
    try { localStorage.setItem('sb-theme', mode) } catch { /* ignore */ }
    dispatchEvent(new CustomEvent('sb-theme'))
  }
  async function install() { menu = false; const p = window.__sbInstall; if (p) { p.prompt(); await p.userChoice; window.__sbInstall = null; canInstall = false } }
  async function logout() { try { await api('auth/logout', { method: 'POST' }) } catch { /* ignore */ } location.reload() }
</script>

<header class="top">
  <div class="grow">
    <h1>{title}</h1>
    {#if sub}<div class="sub">{sub}</div>{/if}
  </div>
  {@render children?.()}
  <button class="iconbtn glass" aria-label="Search (Ctrl+K)" title="Search (Ctrl+K)" onclick={() => dispatchEvent(new CustomEvent('sb-palette'))}>{@html I.search}</button>
  {#if sync}
    <button class="syncchip glass" class:stale class:bad={freshest?.failing} onclick={refresh} disabled={syncing} title="Sync now">
      <span class="spin" class:on={syncing}>{@html I.sync}</span>
      <span class="num">{syncing ? 'Syncing' : freshest?.ts ? ago(freshest.ts) : 'never'}</span>
    </button>
  {/if}
  <div class="menuwrap">
    <button class="iconbtn glass" aria-label="More" aria-expanded={menu} onclick={() => (menu = !menu)}>{@html I.more}</button>
    {#if menu}
      <button class="scrim" aria-label="Close menu" onclick={() => (menu = false)}></button>
      <div class="menu card glass" role="menu">
        <button role="menuitem" onclick={theme}>{@html mode === 'dark' ? I.moon : I.sun}<span>{LABEL[mode]}</span></button>
        {#if canInstall}<button role="menuitem" onclick={install}>{@html I.install}<span>Install app</span></button>{/if}
        <a role="menuitem" href="#/control" onclick={() => (menu = false)}>{@html I.sliders}<span>Control panel</span></a>
        <button role="menuitem" onclick={() => { menu = false; botSheet = true }}>{@html I.gear}<span>Bot settings</span></button>
        <a role="menuitem" href="#/deadlines?ics" onclick={() => (menu = false)}>{@html I.cal}<span>Calendar feed</span></a>
        <button role="menuitem" onclick={logout}>{@html I.logout}<span>Log out</span></button>
      </div>
    {/if}
  </div>
</header>
{#if botSheet}<BotSettings onclose={() => (botSheet = false)} />{/if}

<style>
  .syncchip { display: inline-flex; align-items: center; gap: 6px; height: 44px; padding: 0 14px 0 12px; border-radius: 999px; border: 0; font-size: 13px; font-weight: 500; letter-spacing: -.004em; color: var(--muted); flex: none; }
    @media (min-width: 1024px) { .syncchip { height: 36px; } }
  .syncchip :global(svg) { width: 15px; height: 15px; display: block; }
  .syncchip.stale { color: var(--warn); }
  .syncchip.bad { color: var(--danger); background: var(--danger-bg); }
  .spin.on { animation: rot 1s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  .menuwrap { position: relative; }
  .scrim { position: fixed; inset: 0; background: transparent; border: 0; z-index: 29; }
  .menu { position: absolute; right: 0; top: 50px; z-index: 30; min-width: 240px; padding: 6px 0; transform-origin: top right; animation: popin .22s cubic-bezier(.2, .9, .3, 1.1); }
  @keyframes popin { from { opacity: 0; transform: scale(.92); } }
  .menu button, .menu a { display: flex; flex-direction: row-reverse; justify-content: space-between; gap: 12px; align-items: center; width: 100%; min-height: 44px; padding: 0 16px; border: 0; background: none; text-align: left; font-size: 17px; letter-spacing: -.022em; text-decoration: none; position: relative; }
  .menu > * + *::before { content: ''; position: absolute; top: 0; left: 16px; right: 0; height: 1px; background: var(--line); transform: scaleY(.5); }
  .menu button:hover, .menu a:hover { background: var(--fill); }
  .menu :global(svg) { width: 20px; height: 20px; color: var(--text); flex: none; }
  @media (min-width: 1024px) { .menu button, .menu a { min-height: 34px; font-size: 14px; } }
</style>
