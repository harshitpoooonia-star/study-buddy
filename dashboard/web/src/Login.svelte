<script>
  import { onMount } from 'svelte'
  import { api } from './lib.js'

  let cfg = $state(null)
  let busy = $state(false)
  let msg = $state('')
  let err = $state('')
  let widget

  onMount(async () => {
    try { cfg = await api('auth/config') } catch (e) { err = e.message }
    if (cfg?.bot_username && widget) {
      window.onTelegramAuth = async (user) => {
        try { await api('auth/telegram', { method: 'POST', body: user }); location.reload() } catch (e) { err = e.message }
      }
      const s = document.createElement('script')
      s.src = 'https://telegram.org/js/telegram-widget.js?22'
      s.async = true
      s.dataset.telegramLogin = cfg.bot_username
      s.dataset.size = 'large'
      s.dataset.radius = '10'
      s.dataset.onauth = 'onTelegramAuth(user)'
      s.dataset.requestAccess = 'write'
      widget.appendChild(s)
    }
  })

  async function sendLink() {
    busy = true; err = ''; msg = ''
    try { await api('auth/send-link', { method: 'POST' }); msg = 'Sent - open the link from your Telegram chat with the bot.' }
    catch (e) { err = e.message }
    busy = false
  }
</script>

<div class="wrap">
  <div class="mark" aria-hidden="true">
    <svg viewBox="0 0 512 512"><rect x="112" y="128" width="288" height="256" rx="36" fill="none" stroke="var(--accent)" stroke-width="32"/><path d="M112 200h288" stroke="var(--accent)" stroke-width="32"/><circle cx="208" cy="288" r="26" fill="var(--accent)"/><path d="M262 288h84" stroke="var(--text)" stroke-width="28" stroke-linecap="round"/></svg>
  </div>
  <h1>Study Buddy</h1>
  <p class="muted">Your LMS, attendance and timetable in one place. Private to you.</p>

  {#if cfg?.link_login}
    <button class="btn primary big" onclick={sendLink} disabled={busy}>{busy ? 'Sending…' : 'Send login link to Telegram'}</button>
  {/if}
  {#if msg}<p class="ok">{msg}</p>{/if}
  {#if err}<p class="err">{err}</p>{/if}

  <div class="widget" bind:this={widget}></div>
  {#if cfg && !cfg.link_login && !cfg.bot_username}
    <p class="muted small">Login isn't configured on the server yet. Run deploy_dash.sh on the server.</p>
  {/if}
</div>

<style>
  .wrap { min-height: 100dvh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 24px 16px; max-width: 380px; margin: 0 auto; }
  .mark svg { width: 88px; height: 88px; padding: 10px; border-radius: 22px; background: var(--surface); }
  h1 { margin: 16px 0 6px; font-size: 34px; line-height: 41px; font-weight: 700; letter-spacing: .011em; }
  p { margin: 0 0 24px; }
  .big { width: 100%; min-height: 50px; font-size: 17px; border-radius: 14px; }
  .widget { margin-top: 18px; min-height: 44px; }
  .ok { color: var(--ok); margin-top: 14px; }
  .err { color: var(--danger); margin-top: 14px; }
  .small { font-size: 13px; }
</style>
