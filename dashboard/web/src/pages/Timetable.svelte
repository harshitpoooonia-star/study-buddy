<script>
  import { onMount, tick } from 'svelte'
  import { api, AuthError, hm, dateOf } from '../lib.js'
  import Header from '../Header.svelte'

  let d = $state(null)
  let err = $state('')
  let week = $state('')
  let now = $state(Date.now() / 1000)
  let showFree = $state(true)

  async function load(w = week) {
    try {
      d = await api(`timetable${w ? `?week=${w}` : ''}`); week = d.week; err = ''
      await tick()
      document.getElementById(`day-${d.today}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    } catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  function shift(n) {
    const t = new Date(`${week}T12:00:00+05:30`); t.setDate(t.getDate() + 7 * n)
    load(t.toISOString().slice(0, 10))
  }
  onMount(() => {
    load('')
    const t = setInterval(() => (now = Date.now() / 1000), 30000)
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => { clearInterval(t); removeEventListener('sb-refresh', r) }
  })

  const f = (o) => new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', ...o })
  const wd = f({ weekday: 'long' })
  const dm = f({ day: 'numeric', month: 'short' })
  const range = $derived(d ? `${dm.format(dateOf(d.days[0].date) * 1000)} – ${dm.format(dateOf(d.days[6].date) * 1000)}` : '')
  const isThisWeek = $derived(d && d.days.some((x) => x.date === d.today))
  const total = $derived(d ? d.days.reduce((n, x) => n + x.classes.length, 0) : 0)
  const mins = (s) => +s.slice(0, 2) * 60 + +s.slice(3, 5)
  const dur = (a, b) => { const m = mins(b) - mins(a); return m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}` : `${m}m` }

  // Merge classes and free gaps into one ordered list per day.
  function rows(x) {
    const out = x.classes.map((c) => ({ kind: 'class', key: c.start + c.subject_code, ...c }))
    if (showFree) out.push(...x.free.map((g) => ({ kind: 'free', key: 'f' + g.start, ...g })))
    return out.sort((a, b) => mins(a.start) - mins(b.start))
  }
</script>

<Header title="Timetable" sub={range} sync={d?.sync} keys={['timetable']} />

{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

<div class="weeknav">
  <button class="iconbtn" aria-label="Previous week" onclick={() => shift(-1)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 18l-6-6 6-6"/></svg></button>
  <button class="btn" disabled={isThisWeek} onclick={() => load('')}>This week</button>
  <button class="iconbtn" aria-label="Next week" onclick={() => shift(1)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 18l6-6-6-6"/></svg></button>
  <span class="grow"></span>
  <label class="toggle"><input type="checkbox" bind:checked={showFree} /> Free slots</label>
</div>

{#if !d}
  <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
{:else}
  {#if d.status?.status === 'not_finalised'}
    <div class="banner warn"><span>📝</span><div><b>Timetable not finalised.</b> MyCamu says “{d.status.note || 'No timetable found'}” — usually a mid/end-sem exam week. Check Outlook for the date sheet.</div></div>
  {:else if !d.status && total === 0}
    <div class="banner warn"><div>This week isn't synced. The collector keeps the current and next week; earlier weeks stay once they've been seen.</div></div>
  {/if}

  {#each d.days as x}
    {#if x.classes.length || x.exams.length || x.date === d.today}
      {@const r = rows(x)}
      <section class="day" id={`day-${x.date}`}>
        <div class="dayhead" class:today={x.date === d.today}>
          <span class="dn">{wd.format(dateOf(x.date) * 1000)}</span>
          <span class="dd faint">{dm.format(dateOf(x.date) * 1000)}</span>
          {#if x.date === d.today}<span class="pill info">Today</span>{/if}
          <span class="grow"></span>
          <span class="faint small num">{x.classes.length} class{x.classes.length === 1 ? '' : 'es'}</span>
        </div>
        {#each x.exams as e}
          <div class="exam">
            <b>📝 {e.course_code} exam</b> · {e.detail.time_known ? `${new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit' }).format(e.due_at * 1000)}` : 'time TBC'}
            <span class="faint">{e.title}{e.detail.venue ? ` · ${e.detail.venue}` : ''}</span>
          </div>
        {/each}
        <div class="card">
          {#if !x.classes.length}<div class="empty">No classes.</div>{/if}
          <ul class="list">
            {#each r as c (c.key)}
              {#if c.kind === 'class'}
                <li class="cls" class:past={c.end_ts <= now} class:now={c.start_ts <= now && now < c.end_ts}>
                  <div class="tcol num"><span>{hm(c.start)}</span><span class="faint">{hm(c.end)}</span></div>
                  <div class="grow">
                    <div class="sname">{c.subject}</div>
                    <div class="faint small">{c.subject_code}{c.faculty ? ` · ${c.faculty}` : ''}</div>
                  </div>
                  {#if c.start_ts <= now && now < c.end_ts}<span class="pill ok">Now</span>{/if}
                </li>
              {:else}
                <li class="free"><span class="num">{hm(c.start)} – {hm(c.end)}</span><span class="faint">free · {dur(c.start, c.end)}</span></li>
              {/if}
            {/each}
          </ul>
        </div>
      </section>
    {/if}
  {/each}
  {#if total === 0 && d.status?.status !== 'not_finalised' && d.status}
    <div class="card empty">No classes this week.</div>
  {/if}
{/if}

<style>
  .small { font-size: 13px; }
  .weeknav { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
  .weeknav .btn { padding: 8px 14px; }
  .weeknav svg { width: 18px; height: 18px; }
  .toggle { font-size: 13px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
  .toggle input { accent-color: var(--accent); }
  .day { margin-top: 18px; scroll-margin-top: 12px; }
  .dayhead { display: flex; align-items: baseline; gap: 8px; margin: 0 2px 8px; }
  .dn { font-weight: 650; }
  .dayhead.today .dn { color: var(--accent); }
  .cls { display: flex; gap: 14px; align-items: center; padding: 12px 16px; }
  .tcol { display: flex; flex-direction: column; font-size: 13px; width: 64px; flex: none; line-height: 1.35; }
  .sname { font-weight: 600; line-height: 1.3; }
  .cls.past { opacity: .45; }
  .cls.now { background: color-mix(in srgb, var(--accent) 8%, transparent); box-shadow: inset 3px 0 0 var(--accent); }
  .exam { background: var(--danger-bg); border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--line)); border-radius: 12px; padding: 10px 14px; margin-bottom: 8px; font-size: 14px; display: flex; flex-wrap: wrap; gap: 4px 8px; }
  .free { display: flex; gap: 10px; justify-content: space-between; padding: 8px 16px; font-size: 12.5px; color: var(--muted); background: repeating-linear-gradient(135deg, transparent 0 8px, color-mix(in srgb, var(--line) 45%, transparent) 8px 9px); }
</style>
