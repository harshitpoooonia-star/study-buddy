<script>
  import { onMount } from 'svelte'
  import { api, AuthError, dayLong, day, hm, countdown, short } from '../lib.js'
  import Header from '../Header.svelte'
  import ItemRow from '../ItemRow.svelte'

  let d = $state(null)
  let err = $state('')
  let now = $state(Date.now() / 1000)

  async function load() {
    try {
      d = await api('today'); err = ''
      dispatchEvent(new CustomEvent('sb-overdue', { detail: d.due_soon.filter((i) => i.bucket === 'overdue' && !i.done).length }))
    } catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }

  onMount(() => {
    load()
    const t = setInterval(() => (now = Date.now() / 1000), 1000)
    const r = () => load()
    const vis = () => document.visibilityState === 'visible' && load()
    addEventListener('sb-refresh', r)
    document.addEventListener('visibilitychange', vis)
    return () => { clearInterval(t); removeEventListener('sb-refresh', r); document.removeEventListener('visibilitychange', vis) }
  })

  // Re-derive current/next from the clock so the hero stays right without refetching.
  const cur = $derived(d?.classes.find((c) => c.start_ts <= now && now < c.end_ts) || null)
  const nxt = $derived.by(() => {
    if (!d) return null
    const later = d.classes.find((c) => c.start_ts > now)
    return later || (d.next && d.next.start_ts > now ? d.next : null)
  })
  const pending = $derived(d ? d.due_soon.filter((i) => !i.done) : [])
  const unread = $derived(d ? (d.unread.moodle || 0) + (d.unread.mycamu || 0) : 0)
  const notFinal = $derived(d?.timetable_status?.status === 'not_finalised')
</script>

<Header title="Today" sub={d ? dayLong(d.now) : ''} sync={d?.sync} />

{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

{#if !d}
  <div class="skeleton" style="height:120px"></div><div class="skeleton"></div><div class="skeleton"></div>
{:else}
  {#if notFinal}
    <div class="banner warn"><span>📝</span><div><b>Exam week?</b> MyCamu says the timetable isn't finalised for this week. Check Outlook for the CoE date sheet.</div></div>
  {/if}

  <section class="hero card">
    {#if cur}
      {@const pct = Math.min(100, ((now - cur.start_ts) / (cur.end_ts - cur.start_ts)) * 100)}
      <div class="label"><span class="live"></span>In class now</div>
      <div class="subj">{cur.subject}</div>
      <div class="muted small">{cur.subject_code} · {cur.faculty} · {hm(cur.start)}–{hm(cur.end)}</div>
      <div class="bar"><i style={`width:${pct}%`}></i></div>
      <div class="cd num">{countdown(cur.end_ts, now)} <span class="muted">left</span></div>
      {#if nxt && nxt.date === d.date}<div class="then small muted">Then <b>{short(nxt.subject)}</b> at {hm(nxt.start)}</div>{/if}
    {:else if nxt}
      <div class="label">Next class{nxt.date !== d.date ? ` · ${day(nxt.start_ts)}` : ''}</div>
      <div class="subj">{nxt.subject}</div>
      <div class="muted small">{nxt.subject_code} · {nxt.faculty} · {hm(nxt.start)}–{hm(nxt.end)}</div>
      <div class="cd num">{nxt.start_ts - now < 86400 ? countdown(nxt.start_ts, now) : `${Math.round((nxt.start_ts - now) / 86400)} days`} <span class="muted">to go</span></div>
    {:else}
      <div class="label">Classes</div>
      <div class="subj">{notFinal ? 'No timetable this week' : 'Nothing scheduled'}</div>
      <div class="muted small">Synced timetable covers this week and next.</div>
    {/if}
  </section>

  <div class="stats">
    <a class="stat card" href="#/deadlines">
      <span class="v num" class:hot={pending.some((i) => i.bucket === 'overdue')}>{pending.length}</span>
      <span class="k">due ≤ 48h</span>
    </a>
    <a class="stat card" href="#/attendance">
      <span class="v num" class:hot={d.attendance_overall && d.attendance_overall.pct < 75}>{d.attendance_overall ? `${Math.round(d.attendance_overall.pct)}%` : '—'}</span>
      <span class="k">attendance</span>
    </a>
    <div class="stat card">
      <span class="v num">{unread}</span>
      <span class="k">unread · LMS {d.unread.moodle || 0} · Camu {d.unread.mycamu || 0}</span>
    </div>
  </div>

  {#if d.attendance_warnings.length}
    <div class="section"><h2>Attendance</h2><span class="grow"></span><a href="#/attendance">Details</a></div>
    <div class="chips">
      {#each d.attendance_warnings as w}
        <a class={`achip ${w.level}`} href="#/attendance"><b>{short(w.subject)}</b> <span class="num">{Math.round(w.pct)}%</span> <span class="t">{w.text}</span></a>
      {/each}
    </div>
  {/if}

  <div class="section"><h2>Due soon</h2><span class="count num">{pending.length}</span><span class="grow"></span><a href="#/deadlines">All deadlines</a></div>
  <div class="card">
    {#if d.due_soon.length}
      <ul class="list">{#each d.due_soon as it (it.id)}<ItemRow {it} {now} onchange={load} />{/each}</ul>
    {:else}<div class="empty">Nothing due in the next 48 hours.</div>{/if}
  </div>

  <div class="section"><h2>Today's classes</h2><span class="count num">{d.classes.length}</span><span class="grow"></span><a href="#/timetable">Week</a></div>
  <div class="card">
    {#if d.classes.length}
      <ul class="list">
        {#each d.classes as c}
          <li class="cls" class:past={c.end_ts <= now} class:now={c.start_ts <= now && now < c.end_ts}>
            <span class="t num">{hm(c.start)}</span>
            <div class="grow"><div class="ellipsis">{c.subject}</div><div class="faint small">{c.subject_code} · {c.faculty}</div></div>
            <span class="t num faint">{hm(c.end)}</span>
          </li>
        {/each}
      </ul>
    {:else}<div class="empty">No classes today.</div>{/if}
  </div>
{/if}

<style>
  .small { font-size: 15px; line-height: 20px; letter-spacing: -.012em; }
  @media (min-width: 1024px) { .stats { gap: 16px; } .hero { padding: 22px 24px 24px; } }
  .hero { padding: 16px 18px 18px; position: relative; overflow: hidden; border-radius: 22px; }
  .hero::after { content: ''; position: absolute; right: -40px; top: -40px; width: 140px; height: 140px; border-radius: 50%; background: radial-gradient(circle, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%); pointer-events: none; }
  .label { font-size: 15px; line-height: 20px; letter-spacing: -.012em; color: var(--accent); font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .live { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 0 var(--ok); animation: pulse 1.8s infinite; }
  @keyframes pulse { 70% { box-shadow: 0 0 0 8px transparent; } }
  .subj { font-size: 22px; line-height: 28px; font-weight: 700; margin: 6px 0 2px; letter-spacing: .016em; }
  .cd { font-family: var(--font-round); font-size: 34px; line-height: 41px; font-weight: 700; margin-top: 10px; letter-spacing: 0; }
  .cd .muted { font-size: 15px; font-weight: 500; }
  .bar { height: 6px; background: var(--fill); border-radius: 4px; margin-top: 14px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--accent); }
  .then { margin-top: 4px; }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 12px; }
  .stat { padding: 14px; text-decoration: none; display: flex; flex-direction: column; gap: 4px; min-width: 0; border-radius: 22px; transition: transform .15s; }
  .stat:active { transform: scale(.97); }
  .stat .v { font-family: var(--font-round); font-size: 28px; line-height: 34px; font-weight: 700; letter-spacing: 0; }
  .stat .v.hot { color: var(--danger); }
  .stat .k { font-size: 13px; color: var(--muted); line-height: 18px; letter-spacing: -.004em; }
  .achip { flex: none; text-decoration: none; display: inline-flex; gap: 6px; align-items: baseline; padding: 10px 14px; border-radius: 14px; font-size: 15px; letter-spacing: -.012em; background: var(--surface); }
  .achip.danger { background: var(--danger-bg); }
  .achip.warn { background: var(--warn-bg); }
  .achip.danger .num { color: var(--danger); font-weight: 650; }
  .achip.warn .num { color: var(--warn); font-weight: 650; }
  .achip .t { color: var(--muted); }
  .cls { display: flex; gap: 12px; align-items: center; padding: 11px 16px; min-height: 44px; }
  .cls .t { font-size: 15px; width: 62px; flex: none; }
  .cls .t:last-child { text-align: right; }
  .cls.past { opacity: .45; }
  .cls.now { background: color-mix(in srgb, var(--accent) 8%, transparent); box-shadow: inset 3px 0 0 var(--accent); }
</style>
