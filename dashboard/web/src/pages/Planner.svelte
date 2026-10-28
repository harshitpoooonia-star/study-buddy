<script>
  import { onMount } from 'svelte'
  import { api, AuthError, toast, dateOf, day, dayLong, time, rel } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'

  let d = $state(null)
  let err = $state('')
  let sel = $state('')
  let text = $state('')
  let code = $state('')
  let due = $state('')

  async function load() {
    try { d = await api('planner?weeks=4'); sel = sel || d.today; err = '' }
    catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  onMount(() => {
    load()
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => removeEventListener('sb-refresh', r)
  })

  const weeks = $derived(d ? Array.from({ length: d.days.length / 7 }, (_, i) => d.days.slice(i * 7, i * 7 + 7)) : [])
  const picked = $derived(d?.days.find((x) => x.date === sel))
  const busiest = $derived(d ? [...d.days].filter((x) => x.date >= d.today && x.items.length).sort((a, b) => b.load - a.load)[0] : null)
  const dn = (s) => new Date(`${s}T12:00:00+05:30`).getDate()
  const mon = (s) => new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', month: 'short' }).format(dateOf(s) * 1000)
  const TYPE = { exam: 'Exam', quiz: 'Quiz', assign: 'Assignment', event: 'Event', task: 'Task', todo: 'To-do' }

  async function add() {
    const t = text.trim(); if (!t) return
    try {
      await api('notes', { method: 'POST', body: { text: t, kind: 'todo', course_code: code, due_at: due ? Math.floor(dateOf(due) + 18 * 3600) : null } })
      text = ''; due = ''; await load()
    } catch (e) { toast(e.message) }
  }
  async function done(t) {
    try { await api(`notes/${t.id}`, { method: 'POST', body: { done: true } }); d.todos = d.todos.filter((x) => x.id !== t.id); await load() } catch (e) { toast(e.message) }
  }
  async function del(t) {
    try { await api(`notes/${t.id}`, { method: 'DELETE' }); await load() } catch (e) { toast(e.message) }
  }
</script>

<Header title="Planner" sub="Next 4 weeks" />
{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

{#if !d}
  <div class="skeleton" style="height:220px"></div><div class="skeleton"></div>
{:else}
  <section class="card heat" aria-label="Workload heatmap">
    <div class="wd" aria-hidden="true">{#each ['M', 'T', 'W', 'T', 'F', 'S', 'S'] as w}<span>{w}</span>{/each}</div>
    {#each weeks as wk}
      <div class="wk">
        {#each wk as x}
          <button class={`cell l${x.level}`} class:today={x.date === d.today} class:past={x.date < d.today} class:sel={x.date === sel}
            onclick={() => (sel = x.date)} aria-label={`${dayLong(dateOf(x.date))}: ${x.items.length} due`} aria-pressed={x.date === sel}>
            <span class="dn num">{dn(x.date)}{dn(x.date) === 1 || x === wk[0] && wk === weeks[0] ? ` ${mon(x.date)}` : ''}</span>
            {#if x.items.length}<span class="n num">{x.items.length}</span>{/if}
          </button>
        {/each}
      </div>
    {/each}
    <div class="legend faint small"><span>Light</span>{#each [0, 1, 2, 3, 4] as l}<i class={`l${l}`}></i>{/each}<span>Heavy</span><span class="grow"></span><span>exam &gt; quiz &gt; assignment</span></div>
  </section>

  {#if busiest}
    <p class="faint small tip">Heaviest day: <b>{day(dateOf(busiest.date))}</b> — {busiest.items.length} item{busiest.items.length > 1 ? 's' : ''}. Start the earlier ones a few days ahead.</p>
  {/if}

  <div class="section"><h2>{picked ? dayLong(dateOf(picked.date)) : ''}</h2>{#if picked}<span class="count num">{picked.items.length}</span>{/if}<span class="grow"></span>{#if picked?.classes}<span class="faint small">{picked.classes} classes</span>{/if}</div>
  <div class="card">
    {#if !picked?.items.length}<div class="empty">Nothing due.{picked?.date >= d.today ? ' A good day to get ahead.' : ''}</div>{/if}
    <ul class="list">
      {#each picked?.items || [] as i (i.id)}
        <li class="row it">
          <span class={`pill ${i.type === 'exam' ? 'danger' : i.type === 'quiz' ? 'warn' : 'muted'}`}>{TYPE[i.type] || i.type}</span>
          <span class="grow"><span class="ellipsis tt">{i.title}</span><span class="faint small">{i.course_code || ''} · {time(i.due_at)}</span></span>
          {#if i.overdue}<span class="pill danger">Overdue</span>{/if}
        </li>
      {/each}
    </ul>
  </div>

  <div class="section"><h2>To-dos</h2><span class="count num">{d.todos.length}</span></div>
  <div class="card">
    <form class="add" onsubmit={(e) => { e.preventDefault(); add() }}>
      <input class="input" bind:value={text} placeholder="Add a to-do…" maxlength="2000" aria-label="To-do" />
      <select class="input" bind:value={code} aria-label="Course"><option value="">No course</option>{#each d.courses as c}<option value={c.code}>{c.code} · {c.title}</option>{/each}</select>
      <div class="row">
        <input class="input grow" type="date" bind:value={due} aria-label="Due date" />
        <button class="btn primary" disabled={!text.trim()}>Add</button>
      </div>
    </form>
    {#if d.todos.length}
      <ul class="list">
        {#each d.todos as t (t.id)}
          <li class="row td">
            <input type="checkbox" onchange={() => done(t)} aria-label="Done" />
            <span class="grow"><span class="tt">{t.text}</span><span class="faint small">{t.course_code || 'General'}{t.due_at ? ` · ${day(t.due_at)}` : ''}</span></span>
            <button class="x" aria-label="Delete" onclick={() => del(t)}>{@html I.trash}</button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}

<style>
  .small { font-size: 13px; }
  .heat { padding: 12px; }
  .wd, .wk { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .wd { margin-bottom: 6px; text-align: center; font-size: 11px; color: var(--faint); font-weight: 600; }
  .wk { margin-bottom: 6px; }
  .cell { position: relative; aspect-ratio: 1 / 1; border-radius: 10px; border: 1px solid transparent; padding: 4px 5px; text-align: left; display: flex; flex-direction: column; justify-content: space-between; background: var(--surface-2); }
  .dn { font-size: 11px; color: var(--muted); line-height: 1.1; }
  .n { font-size: 15px; font-weight: 700; align-self: flex-end; line-height: 1; }
  .l1, .cell.l1 { background: color-mix(in srgb, var(--accent) 16%, var(--surface-2)); }
  .l2, .cell.l2 { background: color-mix(in srgb, var(--accent) 34%, var(--surface-2)); }
  .l3, .cell.l3 { background: color-mix(in srgb, var(--accent) 58%, var(--surface-2)); }
  .l4, .cell.l4 { background: color-mix(in srgb, var(--danger) 70%, var(--surface-2)); }
  .l3 .dn, .l4 .dn { color: var(--text); }
  .l0 { background: var(--surface-2); }
  .cell.past { opacity: .45; }
  .cell.today { border-color: var(--text); }
  .cell.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
  .legend { display: flex; align-items: center; gap: 5px; margin-top: 8px; }
  .legend i { width: 14px; height: 14px; border-radius: 4px; display: inline-block; }
  .tip { margin: 10px 2px 0; }
  .it, .td { padding: 11px 16px; }
  .tt { display: block; line-height: 1.3; }
  .td .tt { white-space: pre-wrap; word-break: break-word; }
  .td input[type=checkbox] { accent-color: var(--accent); width: 18px; height: 18px; flex: none; }
  .add { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--line); }
  .add .input { width: 100%; }
  .add .row .input { width: auto; }
  .x { background: none; border: 0; color: var(--faint); padding: 2px; }
  .x :global(svg) { width: 16px; height: 16px; }
</style>
