<script>
  import { onMount } from 'svelte'
  import { api, AuthError, toast, ago } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'
  import BotSettings from '../BotSettings.svelte'

  let c = $state(null)
  let health = $state(null)
  let st = $state(null)
  let err = $state('')
  let botSheet = $state(false)
  let busy = $state('')

  async function load() {
    try {
      ;[c, health, st] = await Promise.all([api('control'), api('health'), api('settings')])
      err = ''
    } catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  onMount(() => {
    load()
    const t = setInterval(load, 30000)
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => { clearInterval(t); removeEventListener('sb-refresh', r) }
  })

  const NAMES = { moodle: 'LMS deadlines', attendance: 'Attendance', timetable: 'Timetable', camu_unread: 'MyCamu unread', grades: 'Grades', courses: 'Course contents', feed: 'Feed', index: 'Search index' }
  const GROUP = { attendance: 'MyCamu', timetable: 'MyCamu', camu_unread: 'MyCamu' }
  const pct = $derived(c && c.index.total ? Math.round((100 * c.index.ok) / c.index.total) : 0)

  async function act(action, label) {
    busy = action
    try {
      const r = await api('control/action', { method: 'POST', body: { action } })
      toast(r.started ? `${label} started — refresh in a minute` : r.note || 'Queued')
    } catch (e) { toast(e.message) }
    busy = ''
  }
  async function mute(code) {
    const m = st.muted_courses.includes(code) ? st.muted_courses.filter((x) => x !== code) : [...st.muted_courses, code]
    st.muted_courses = m
    try { await api('settings', { method: 'POST', body: { muted_courses: m } }); toast(m.includes(code) ? `${code} muted` : `${code} unmuted`) } catch (e) { toast(e.message) }
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('Copied') } catch { toast('Copy failed — select and copy it manually') }
  }
  const dot = (s) => (s.ok === false ? 'bad' : s.ok ? 'ok' : 'none')
  const up = (s) => { const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600); return d ? `${d}d ${h}h` : `${h}h ${Math.floor((s % 3600) / 60)}m` }
</script>

<Header title="Control panel" sub="Health, search index, bot" sync={health?.sync} keys={['moodle', 'attendance', 'timetable']} />
{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

{#if !c}
  <div class="skeleton" style="height:180px"></div><div class="skeleton"></div>
{:else}
  {#if c.camu.state === 'expired'}
    <div class="banner danger"><span>🔑</span><div><b>MyCamu session expired.</b> Attendance and timetable are stale. Run <code>~/moodle-mcp/set-camu-cookie</code> on the server to paste a fresh cookie.</div></div>
  {:else if c.camu.state === 'error'}
    <div class="banner warn"><div><b>MyCamu is failing.</b> {c.camu.error}</div></div>
  {/if}

  <div class="section"><h2>Sources</h2><span class="grow"></span><span class="faint small">last full run {c.last_run ? ago(c.last_run) : 'never'}</span></div>
  <div class="card">
    <ul class="list">
      {#each c.sources as s}
        <li class="src">
          <div class="row">
            <span class={`sd ${dot(s)}`} aria-hidden="true"></span>
            <span class="grow"><b>{NAMES[s.name] || s.name}</b>{#if GROUP[s.name]} <span class="faint small">· {GROUP[s.name]}</span>{/if}</span>
            <span class="faint small num">{s.last_ok ? ago(s.last_ok) : 'never'}</span>
          </div>
          <div class="faint small meta num">{s.runs24} runs/24h · {s.fails24} failed · avg {s.avg_ms >= 1000 ? `${(s.avg_ms / 1000).toFixed(1)}s` : `${s.avg_ms}ms`}</div>
          {#if s.error}<div class="err small">{s.error}</div>{/if}
        </li>
      {/each}
    </ul>
  </div>

  <div class="section"><h2>Search index</h2><span class="grow"></span><span class="faint small num">{c.counts.docs} documents{c.fts ? '' : ' · LIKE mode'}</span></div>
  <div class="card pad">
    <div class="row"><b class="num">{c.index.ok}</b><span class="muted">of {c.index.total} files readable</span><span class="grow"></span><span class="num faint">{pct}%</span></div>
    <div class="bar" role="progressbar" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100"><i style={`width:${pct}%`}></i></div>
    <div class="faint small">{c.index.todo} waiting · {c.index.skipped} skipped (scanned PDFs, big files, unsupported types). Five files are read every 30 minutes.</div>
    <div class="row btns">
      <button class="btn" disabled={!!busy} onclick={() => act('refresh_courses', 'Refresh')}>Refresh courses</button>
      <button class="btn" disabled={!!busy} onclick={() => act('retry_index', 'Retry')}>Retry failed</button>
      <button class="btn" disabled={!!busy} onclick={() => act('reindex_all', 'Rebuild')}>Rebuild all</button>
    </div>
  </div>

  <div class="section"><h2>Bot</h2></div>
  <div class="card">
    <div class="row pad">
      <div class="grow"><b>Telegram alerts</b><div class="faint small">{st.alerts_paused ? 'Paused' : `On · digest ${st.digest_time} · pings ${st.alert_hours.length ? st.alert_hours.map((h) => `${h}h`).join(' + ') : 'off'}`}</div></div>
      <button class="btn" onclick={() => (botSheet = true)}>{@html I.gear} Settings</button>
    </div>
    <div class="mutes">
      <div class="faint small">Mute a course (no pings or new-post alerts)</div>
      <div class="chips wrap">
        {#each st.courses as co}<button class="chip" class:on={st.muted_courses.includes(co.code)} onclick={() => mute(co.code)} title={co.name}>{st.muted_courses.includes(co.code) ? '🔇 ' : ''}{co.code}</button>{/each}
      </div>
    </div>
  </div>

  <div class="section"><h2>Ask (AI)</h2></div>
  <div class="card pad">
    {#if c.ai.configured}
      <div class="row"><b class="num">{c.ai.used}/{c.ai.limit}</b><span class="muted">questions today (shared with the bot)</span></div>
      <div class="faint small">Groq free tier · {c.ai.models.join(' → ')}</div>
    {:else}
      <div class="muted">Off — add <code>GROQ_API_KEY</code> to <code>.env</code> on the VPS (free at console.groq.com/keys) and restart moodle-mcp.</div>
    {/if}
  </div>

  <div class="section"><h2>Server</h2></div>
  <div class="card pad sys num">
    <div><span class="faint">Memory free</span><b>{c.system.available_mb} / {c.system.total_mb} MB</b></div>
    <div><span class="faint">Swap used</span><b>{c.system.swap_used_mb} / {c.system.swap_total_mb} MB</b></div>
    <div><span class="faint">Dashboard process</span><b>{c.system.process_rss_mb} MB</b></div>
    {#if c.system.cgroup_mb != null}<div><span class="faint">moodle-mcp cgroup</span><b>{c.system.cgroup_mb} MB{c.system.cgroup_max_mb ? ` / ${c.system.cgroup_max_mb}` : ''}</b></div>{/if}
    <div><span class="faint">Database</span><b>{c.system.db_mb} MB</b></div>
    <div><span class="faint">Disk free</span><b>{c.system.disk_free_gb} GB</b></div>
    <div><span class="faint">Up</span><b>{up(c.system.uptime_s)}</b></div>
  </div>

  {#if health?.ics_url}
    <div class="section"><h2>Calendar feed</h2></div>
    <div class="card pad">
      <div class="faint small">Subscribe in Google/Apple Calendar. The link is private — anyone with it can read your deadlines.</div>
      <div class="row ics"><code class="grow ellipsis">{health.ics_url}</code><button class="iconbtn" aria-label="Copy" onclick={() => copy(health.ics_url)}>{@html I.copy}</button></div>
    </div>
  {/if}

  {#if health?.recent_errors?.length}
    <div class="section"><h2>Recent errors</h2></div>
    <div class="card"><ul class="list">
      {#each health.recent_errors as e}<li class="pad"><div class="row"><b>{NAMES[e.source] || e.source}</b><span class="grow"></span><span class="faint small">{ago(e.ran_at)}</span></div><div class="err small">{e.error}</div></li>{/each}
    </ul></div>
  {/if}
{/if}
{#if botSheet}<BotSettings onclose={() => { botSheet = false; load() }} />{/if}

<style>
  .small { font-size: 13px; }
  code { background: var(--surface-2); padding: 1px 5px; border-radius: 5px; font-size: .9em; }
  .src { padding: 11px 16px; }
  .meta { margin: 3px 0 0 20px; }
  .err { margin: 6px 0 0 20px; color: var(--danger); word-break: break-word; }
  .sd { width: 10px; height: 10px; border-radius: 50%; background: var(--faint); flex: none; }
  .sd.ok { background: var(--ok); } .sd.bad { background: var(--danger); }
  .bar { height: 8px; border-radius: 8px; background: var(--surface-2); margin: 10px 0 8px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--ok); border-radius: 8px; }
  .btns { margin-top: 12px; flex-wrap: wrap; }
  .btns .btn { padding: 8px 12px; font-size: 13px; }
  .mutes { padding: 4px 16px 14px; }
  .mutes .chips { margin-top: 8px; flex-wrap: wrap; overflow: visible; }
  .sys { display: grid; gap: 8px; }
  .sys div { display: flex; justify-content: space-between; font-size: 14px; }
  .ics { margin-top: 10px; }
  .ics code { padding: 8px 10px; }
  .btn :global(svg) { width: 15px; height: 15px; vertical-align: -2px; }
</style>
