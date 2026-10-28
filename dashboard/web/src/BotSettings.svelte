<script>
  import { onMount } from 'svelte'
  import { api, toast } from './lib.js'

  let { onclose } = $props()
  let s = $state(null)
  let saving = $state(false)
  const HOURS = [72, 48, 24, 12, 6, 3, 1]

  onMount(async () => {
    try { s = await api('settings') } catch (e) { toast(e.message); onclose() }
  })

  function toggleHour(h) {
    const on = s.alert_hours.includes(h)
    if (!on && s.alert_hours.length >= 3) { toast('Pick up to 3 reminder times'); return }
    s.alert_hours = on ? s.alert_hours.filter((x) => x !== h) : [...s.alert_hours, h].sort((a, b) => b - a)
  }
  function toggleMute(c) {
    s.muted_courses = s.muted_courses.includes(c) ? s.muted_courses.filter((x) => x !== c) : [...s.muted_courses, c]
  }
  async function save() {
    saving = true
    try {
      await api('settings', { method: 'POST', body: {
        alerts_paused: s.alerts_paused, digest_time: s.digest_time, alert_hours: s.alert_hours, muted_courses: s.muted_courses,
      } })
      toast('Saved — the bot picks this up within a minute')
      onclose()
    } catch (e) { toast(e.message) }
    saving = false
  }
</script>

<button class="sheet-scrim" aria-label="Close" onclick={onclose}></button>
<div class="sheet glass" role="dialog" aria-modal="true" aria-label="Bot settings">
  <div class="grab"></div>
  <h3>Bot settings</h3>
  <p class="muted small">What the Telegram bot sends you automatically.</p>
  {#if !s}
    <div class="skeleton"></div>
  {:else}
    <div class="field">
      <div class="lbl">Automatic messages<div class="hint">Digest, deadline and exam pings, new LMS posts</div></div>
      <input class="switch" type="checkbox" checked={!s.alerts_paused} onchange={(e) => (s.alerts_paused = !e.currentTarget.checked)} aria-label="Automatic messages" />
    </div>
    <div class="field">
      <label for="dt">Morning digest<div class="hint">IST</div></label>
      <input id="dt" class="input" type="time" bind:value={s.digest_time} />
    </div>
    <div class="field col">
      <div class="lbl">Remind me before deadlines and exams<div class="hint">Up to 3</div></div>
      <div class="chips wrap">
        {#each HOURS as h}
          <button class="chip" class:on={s.alert_hours.includes(h)} onclick={() => toggleHour(h)}>{h}h</button>
        {/each}
      </div>
    </div>
    <div class="field col">
      <div class="lbl">Mute courses<div class="hint">No pings or new-post alerts for these</div></div>
      <div class="chips wrap">
        {#each s.courses as c}
          <button class="chip" class:on={s.muted_courses.includes(c.code)} title={c.name} onclick={() => toggleMute(c.code)}>{c.code}</button>
        {/each}
      </div>
    </div>
    <div class="actions">
      <button class="btn" onclick={onclose}>Cancel</button>
      <button class="btn primary" onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
    </div>
  {/if}
</div>

<style>
  .small { font-size: 13px; margin: 0 0 8px; }
  .col { flex-direction: column; align-items: stretch; }
  .wrap { flex-wrap: wrap; overflow: visible; }
  .actions { display: flex; gap: 10px; justify-content: flex-end; padding-top: 14px; border-top: 1px solid var(--line); }
</style>
