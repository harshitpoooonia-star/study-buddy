<script>
  import { onMount, tick } from 'svelte'
  import { api, AuthError, toast } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'
  import ItemRow from '../ItemRow.svelte'

  const GROUPS = [
    { id: 'overdue', label: 'Overdue' },
    { id: 'today', label: 'Today' },
    { id: 'week', label: 'This week' },
    { id: 'later', label: 'Later' },
  ]

  let d = $state(null)
  let err = $state('')
  let now = $state(Date.now() / 1000)
  let view = $state(pref('sb-dl-view', 'timeline'))
  let course = $state('')
  let hideDone = $state(pref('sb-dl-hide', '1') === '1')
  let showPast = $state(false)
  let adding = $state(false)
  let ics = $state(null)
  let form = $state({ title: '', course_code: '', when: '', kind: 'task' })
  let icsEl

  function pref(k, dflt) { try { return localStorage.getItem(k) ?? dflt } catch { return dflt } }
  function save(k, v) { try { localStorage.setItem(k, v) } catch { /* ignore */ } }

  async function load() {
    try {
      d = await api('items'); err = ''
      dispatchEvent(new CustomEvent('sb-overdue', { detail: d.items.filter((i) => i.bucket === 'overdue' && !i.done).length }))
    } catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }

  onMount(() => {
    load()
    api('health').then((h) => (ics = h.ics_url)).catch(() => {})
    if (location.hash.includes('ics')) tick().then(() => setTimeout(() => icsEl?.scrollIntoView({ behavior: 'smooth' }), 400))
    const t = setInterval(() => (now = Date.now() / 1000), 30000)
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => { clearInterval(t); removeEventListener('sb-refresh', r) }
  })

  const visible = $derived.by(() => {
    if (!d) return []
    return d.items.filter((i) => (!course || i.course_code === course)
      && !(i.snoozed_until && i.snoozed_until > now && i.bucket !== 'overdue'))
  })
  const grouped = $derived(Object.fromEntries(GROUPS.map((g) => [g.id,
    visible.filter((i) => i.bucket === g.id && !(hideDone && i.done))])))
  const past = $derived(visible.filter((i) => i.bucket === 'past').reverse())
  const snoozed = $derived(d ? d.items.filter((i) => i.snoozed_until && i.snoozed_until > now).length : 0)

  async function add(e) {
    e.preventDefault()
    if (!form.title.trim()) return
    const due = form.when ? Math.floor(new Date(`${form.when}:00+05:30`).getTime() / 1000) : null
    try {
      await api('entries', { method: 'POST', body: { title: form.title, course_code: form.course_code, due_at: due, kind: form.kind } })
      form = { title: '', course_code: '', when: '', kind: 'task' }
      adding = false
      toast('Added - it shows in your calendar feed too')
      load()
    } catch (e2) { toast(e2.message) }
  }

  async function copy() {
    try { await navigator.clipboard.writeText(ics); toast('Feed URL copied') } catch { toast('Copy failed - long-press the URL instead') }
  }
  const gcal = $derived(ics ? `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(ics.replace(/^https?:/, 'webcal:'))}` : '')
</script>

<Header title="Deadlines" sub={d ? `${visible.filter((i) => !i.done && i.bucket !== 'past').length} open` : ''} sync={d?.sync} />

{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

<div class="controls">
  <div class="seg" role="tablist">
    <button class:on={view === 'timeline'} onclick={() => { view = 'timeline'; save('sb-dl-view', view) }}>Timeline</button>
    <button class:on={view === 'board'} onclick={() => { view = 'board'; save('sb-dl-view', view) }}>Board</button>
  </div>
  <label class="toggle"><input type="checkbox" bind:checked={hideDone} onchange={() => save('sb-dl-hide', hideDone ? '1' : '0')} /> Hide done</label>
  <span class="grow"></span>
  <button class="btn add" onclick={() => (adding = !adding)}>{@html I.plus}<span>Add</span></button>
</div>

{#if d?.courses?.length}
  <div class="chips filt">
    <button class="chip" class:on={!course} onclick={() => (course = '')}>All</button>
    {#each d.courses as c}
      <button class="chip" class:on={course === c.code} onclick={() => (course = course === c.code ? '' : c.code)} title={c.name}>{c.code}</button>
    {/each}
  </div>
{/if}

{#if adding}
  <form class="card pad addform" onsubmit={add}>
    <input placeholder="What's due? e.g. CN mid-sem, viva prep" bind:value={form.title} required maxlength="200" />
    <div class="row2">
      <select bind:value={form.course_code} aria-label="Course">
        <option value="">No course</option>
        {#each d?.courses || [] as c}<option value={c.code}>{c.code}</option>{/each}
      </select>
      <select bind:value={form.kind} aria-label="Kind"><option value="task">Task</option><option value="exam">Exam</option></select>
    </div>
    <input type="datetime-local" bind:value={form.when} aria-label="Due (IST)" />
    <div class="row"><span class="faint small grow">Times are IST. Only you see this.</span><button class="btn primary">Save</button></div>
  </form>
{/if}

{#if !d}
  <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
{:else if view === 'timeline'}
  {#each GROUPS as g}
    {#if grouped[g.id].length || g.id === 'today'}
      <div class="section"><h2 class:red={g.id === 'overdue'}>{g.label}</h2><span class="count num">{grouped[g.id].length}</span></div>
      <div class="card">
        {#if grouped[g.id].length}
          <ul class="list">{#each grouped[g.id] as it (it.id)}<ItemRow {it} {now} onchange={load} />{/each}</ul>
        {:else}<div class="empty">Nothing due today.</div>{/if}
      </div>
    {/if}
  {/each}
  {#if past.length}
    <div class="section"><h2>Past</h2><span class="count num">{past.length}</span><span class="grow"></span><button class="link" onclick={() => (showPast = !showPast)}>{showPast ? 'Hide' : 'Show'}</button></div>
    {#if showPast}<div class="card"><ul class="list">{#each past as it (it.id)}<ItemRow {it} {now} onchange={load} />{/each}</ul></div>{/if}
  {/if}
{:else}
  <div class="board">
    {#each GROUPS as g}
      <div class="col">
        <div class="colhead"><span class:red={g.id === 'overdue'}>{g.label}</span><span class="faint num">{grouped[g.id].length}</span></div>
        <div class="card">
          {#if grouped[g.id].length}
            <ul class="list">{#each grouped[g.id] as it (it.id)}<ItemRow {it} {now} onchange={load} />{/each}</ul>
          {:else}<div class="empty">—</div>{/if}
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if snoozed}<p class="faint small center">{snoozed} snoozed item{snoozed > 1 ? 's' : ''} hidden until tomorrow.</p>{/if}

<div class="section" bind:this={icsEl}><h2>Calendar feed</h2></div>
<div class="card pad ics">
  {#if ics}
    <p class="small muted">Subscribe once and every LMS deadline (plus your own entries) shows up in your phone calendar, refreshed about hourly.</p>
    <div class="url num" title={ics}>{ics.replace(/[A-Za-z0-9_-]{20,}(?=\.ics)/, (m) => m.slice(0, 6) + '…')}</div>
    <div class="row wrap">
      <a class="btn primary" href={gcal} target="_blank" rel="noopener">Add to Google Calendar</a>
      <a class="btn" href={ics.replace(/^https?:/, 'webcal:')}>Apple / other</a>
      <button class="btn" onclick={copy}>{@html I.copy} Copy URL</button>
    </div>
    <p class="faint small">Keep this URL private - anyone with it can read your deadlines.</p>
  {:else}
    <p class="small muted">Feed isn't configured on the server (DASH_ICS_TOKEN).</p>
  {/if}
</div>

<style>
  .controls { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .toggle { font-size: 13px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
  .toggle input { accent-color: var(--accent); }
  .add { display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; }
  .add :global(svg) { width: 16px; height: 16px; }
  .filt { margin-bottom: 6px; }
  .red { color: var(--danger) !important; }
  .addform { display: grid; gap: 10px; margin: 12px 0 4px; }
  .addform input, .addform select { width: 100%; background: var(--fill); border: 0; border-radius: 10px; padding: 10px 12px; min-height: 44px; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .small { font-size: 13px; }
  .center { text-align: center; }
  .board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(280px, 85%); gap: 12px; overflow-x: auto; scroll-snap-type: x mandatory; margin: 14px -16px 0; padding: 0 16px 8px; }
  @media (min-width: 720px) { .board { grid-auto-columns: minmax(300px, 1fr); } }
  .col { scroll-snap-align: start; min-width: 0; }
  .colhead { display: flex; justify-content: space-between; font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; margin: 0 2px 8px; }
  .ics p { margin: 0 0 10px; }
  .url { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; background: var(--surface-2); padding: 8px 10px; border-radius: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 10px; }
  .wrap { flex-wrap: wrap; margin-bottom: 8px; }
  .ics .btn { text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }
  .ics .btn :global(svg) { width: 16px; height: 16px; }
</style>
