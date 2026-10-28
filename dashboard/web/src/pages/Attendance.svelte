<script>
  import { onMount } from 'svelte'
  import { api, AuthError, day, dateOf, hm, short } from '../lib.js'
  import Header from '../Header.svelte'

  let d = $state(null)
  let err = $state('')
  let sort = $state('risk')
  let sim = $state(false)
  let skip = $state(new Set())
  const keyOf = (u) => `${u.date} ${u.start} ${u.subject_code}`
  function toggle(u) { const n = new Set(skip); n.has(keyOf(u)) ? n.delete(keyOf(u)) : n.add(keyOf(u)); skip = n }
  function toggleDay(list) {
    const n = new Set(skip); const all = list.every((u) => n.has(keyOf(u)))
    list.forEach((u) => (all ? n.delete(keyOf(u)) : n.add(keyOf(u)))); skip = n
  }
  // Group upcoming synced classes by day for the planner.
  const byDay = $derived.by(() => {
    const m = new Map()
    for (const u of d?.upcoming || []) { if (!m.has(u.date)) m.set(u.date, []); m.get(u.date).push(u) }
    return [...m.entries()]
  })
  // Projection for one subject after the synced window, attending everything except the ticked classes.
  function project(sbj) {
    const t = d.target / 100
    const ups = (d.upcoming || []).filter((u) => u.subject_code === sbj.subject_code)
    const missed = ups.filter((u) => skip.has(keyOf(u))).length
    const p = sbj.present + ups.length - missed, n = sbj.total + ups.length
    const pct = n ? Math.round((1000 * p) / n) / 10 : 0
    const must = n && p / n < t ? Math.ceil((t * n - p) / (1 - t)) : 0
    const can = n && p / n >= t ? Math.floor(p / t - n + 1e-9) : 0
    return { pct, missed, classes: ups.length, must, can, delta: Math.round((pct - sbj.pct) * 10) / 10 }
  }

  async function load() {
    try { d = await api('attendance'); err = '' }
    catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  onMount(() => {
    load()
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => removeEventListener('sb-refresh', r)
  })

  const subjects = $derived.by(() => {
    if (!d) return []
    const s = [...d.subjects]
    if (sort === 'risk') s.sort((a, b) => (b.must_attend - a.must_attend) || (a.can_skip - b.can_skip) || (a.pct - b.pct))
    else s.sort((a, b) => a.subject.localeCompare(b.subject))
    return s
  })
  const tone = (p, t) => (p < t ? 'danger' : p < t + 5 ? 'warn' : 'ok')
  const R = 52
  const C = 2 * Math.PI * R
</script>

<Header title="Attendance" sub={d?.semester || ''} sync={d?.sync} />

{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

{#if !d}
  <div class="skeleton" style="height:140px"></div><div class="skeleton"></div><div class="skeleton"></div>
{:else if !d.subjects.length}
  <div class="card empty">No attendance synced yet. If MyCamu's session expired, run SET_CAMU_COOKIE.bat.</div>
{:else}
  {@const o = d.overall}
  {#if o}
    <section class="card overall">
      <svg viewBox="0 0 120 120" class="ring" aria-hidden="true">
        <circle cx="60" cy="60" r={R} class="track" />
        <circle cx="60" cy="60" r={R} class={`val ${tone(o.pct, d.target)}`} stroke-dasharray={`${(C * Math.min(o.pct, 100)) / 100} ${C}`} />
        <line x1="104" y1="60" x2="118" y2="60" class="tick" transform={`rotate(${3.6 * d.target} 60 60)`} />
      </svg>
      <div class="ov">
        <div class={`big num ${tone(o.pct, d.target)}`}>{Math.round(o.pct * 10) / 10}%</div>
        <div class="muted">{o.present} of {o.total} classes overall</div>
        <div class="faint small">Target {d.target}% per subject · tick marks the line</div>
      </div>
    </section>
  {/if}

  <div class="section">
    <h2>Subjects</h2><span class="count num">{d.subjects.length}</span><span class="grow"></span>
    <button class="btn whatif" class:on={sim} onclick={() => { sim = !sim; if (!sim) skip = new Set() }}>{sim ? 'Done' : 'What if…'}</button>
    <div class="seg small"><button class:on={sort === 'risk'} onclick={() => (sort = 'risk')}>At risk</button><button class:on={sort === 'name'} onclick={() => (sort = 'name')}>A–Z</button></div>
  </div>

  {#if sim}
    <section class="card plan">
      <div class="row planhead">
        <div class="grow"><b>Plan your absences</b><div class="faint small">Tap the classes you'll miss. Every subject below updates.</div></div>
        <span class="pill {skip.size ? 'warn' : 'muted'} num">{skip.size} skipped</span>
      </div>
      {#if !byDay.length}
        <div class="empty">No upcoming classes in the synced timetable (this week + next).</div>
      {/if}
      {#each byDay as [dt, list]}
        <div class="pday">
          <button class="pdayh" onclick={() => toggleDay(list)}>
            <span>{day(dateOf(dt))}</span><span class="faint small">{list.every((u) => skip.has(keyOf(u))) ? 'Attend all' : 'Skip whole day'}</span>
          </button>
          <div class="slots">
            {#each list as u}
              <button class="slot" class:off={skip.has(keyOf(u))} onclick={() => toggle(u)} aria-pressed={skip.has(keyOf(u))}>
                <span class="num t">{hm(u.start)}</span><b>{short(u.subject)}</b>
              </button>
            {/each}
          </div>
        </div>
      {/each}
    </section>
  {/if}

  <div class="grid">
    {#each subjects as s (s.subject_code)}
      {@const t = tone(s.pct, d.target)}
      <article class="card subj">
        <div class="row">
          <div class="grow">
            <div class="code">{s.subject_code}</div>
            <div class="name">{s.subject}</div>
          </div>
          <div class={`pct num ${t}`}>{Math.round(s.pct * 10) / 10}<small>%</small></div>
        </div>
        <div class="gauge" role="img" aria-label={`${s.pct}% against ${d.target}% target`}>
          <i class={t} style={`width:${Math.min(100, s.pct)}%`}></i>
          <b style={`left:${d.target}%`}></b>
        </div>
        <div class="row foot">
          <span class="num muted">{s.present}/{s.total}</span>
          <span class="grow"></span>
          {#if s.total === 0}<span class="pill muted">No classes yet</span>
          {:else if s.must_attend > 0}<span class="pill danger">Attend next {s.must_attend}</span>
          {:else if s.can_skip > 0}<span class={`pill ${s.can_skip <= 2 ? 'warn' : 'ok'}`}>Can skip {s.can_skip}</span>
          {:else}<span class="pill warn">On the line</span>{/if}
        </div>
        {#if sim && s.upcoming_classes > 0}
          {@const pr = project(s)}
          <div class="proj small sim">
            After the next {pr.classes} class{pr.classes > 1 ? 'es' : ''}{pr.missed ? `, missing ${pr.missed}` : ''}:
            <b class={`num ${tone(pr.pct, d.target)}`}>{pr.pct}%</b>
            <span class="num" class:up={pr.delta > 0} class:down={pr.delta < 0}>{pr.delta > 0 ? '+' : ''}{pr.delta}</span>
            · {pr.must ? `then attend ${pr.must} more` : pr.can ? `then can skip ${pr.can}` : 'right on the line'}
          </div>
        {:else if sim}
          <div class="proj small sim faint">No classes for this subject in the synced timetable.</div>
        {:else if s.upcoming_classes > 0}
          <div class="proj small">
            Next {s.upcoming_classes} class{s.upcoming_classes > 1 ? 'es' : ''}{d.timetable_until ? ` (to ${day(dateOf(d.timetable_until))})` : ''}:
            attend all → <b class={`num ${tone(s.if_attend_all, d.target)}`}>{s.if_attend_all}%</b>,
            skip all → <b class={`num ${tone(s.if_skip_all, d.target)}`}>{s.if_skip_all}%</b>
          </div>
        {/if}
      </article>
    {/each}
  </div>

  <p class="faint small note">Uses MyCamu's own percentages. Medical/OD adjustments are applied by the university, so the official figure can differ. "Attend next N" means N classes in a row with no absences.</p>
{/if}

<style>
  .small { font-size: 13px; }
  .overall { display: flex; gap: 18px; align-items: center; padding: 16px 18px; }
  .ring { width: 104px; height: 104px; flex: none; transform: rotate(-90deg); }
  .ring circle { fill: none; stroke-width: 10; }
  .ring .track { stroke: var(--surface-2); }
  .ring .val { stroke-linecap: round; }
  .ring .tick { stroke: var(--text); stroke-width: 3; }
  .val.danger { stroke: var(--danger); } .val.warn { stroke: var(--warn); } .val.ok { stroke: var(--ok); }
  .big { font-size: 34px; font-weight: 750; letter-spacing: -.02em; line-height: 1.05; }
  .danger { color: var(--danger); } .warn { color: var(--warn); } .ok { color: var(--ok); }
  .seg.small button { padding: 4px 10px; font-size: 12px; }
  .grid { display: grid; gap: 10px; }
  @media (min-width: 640px) { .grid { grid-template-columns: 1fr 1fr; } }
  .subj { padding: 14px 16px; }
  .name { font-weight: 600; line-height: 1.3; margin-top: 1px; }
  .pct { font-size: 26px; font-weight: 750; letter-spacing: -.02em; }
  .pct small { font-size: 14px; font-weight: 600; }
  .gauge { position: relative; height: 8px; background: var(--surface-2); border-radius: 8px; margin: 12px 0 10px; }
  .gauge i { position: absolute; inset: 0 auto 0 0; border-radius: 8px; }
  .gauge i.danger { background: var(--danger); } .gauge i.warn { background: var(--warn); } .gauge i.ok { background: var(--ok); }
  .gauge b { position: absolute; top: -4px; width: 2px; height: 16px; background: var(--text); border-radius: 2px; transform: translateX(-1px); }
  .foot { font-size: 13px; }
  .proj { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); color: var(--muted); }
  .note { margin-top: 16px; }
  .whatif { padding: 5px 12px; font-size: 13px; margin-right: 8px; }
  .whatif.on { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  .plan { padding: 14px 16px; margin-bottom: 12px; }
  .planhead { margin-bottom: 6px; }
  .pday { padding: 8px 0; border-top: 1px solid var(--line); }
  .pdayh { display: flex; justify-content: space-between; width: 100%; background: none; border: 0; padding: 2px 0 8px; font-weight: 600; font-size: 14px; }
  .slots { display: flex; flex-wrap: wrap; gap: 6px; }
  .slot { display: inline-flex; gap: 6px; align-items: baseline; border: 0; background: var(--fill); border-radius: 10px; padding: 7px 12px; font-size: 15px; }
  .slot .t { color: var(--muted); font-size: 12px; }
  .slot.off { background: var(--danger-bg); border-color: color-mix(in srgb, var(--danger) 50%, var(--line)); text-decoration: line-through; text-decoration-color: var(--danger); }
  .slot.off b { color: var(--danger); }
  .sim { color: var(--text); }
  .up { color: var(--ok); } .down { color: var(--danger); }
</style>
