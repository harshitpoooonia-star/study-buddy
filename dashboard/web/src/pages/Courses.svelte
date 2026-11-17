<script>
  import { onMount, tick } from 'svelte'
  import { api, AuthError, toast, hl, debounce, rel, day, kb, hashQuery } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'
  import FileSheet from '../FileSheet.svelte'

  let { arg = '' } = $props()
  const cid = $derived(arg ? Number(arg) : 0)

  let list = $state(null)
  let d = $state(null)
  let err = $state('')
  let q = $state(hashQuery().get('q') || '')
  let results = $state(null)
  let sheet = $state(null)
  let open = $state({})
  let draft = $state('')
  let kind = $state('todo')

  const MOD = { resource: 'File', folder: 'Folder', assign: 'Assignment', quiz: 'Quiz', forum: 'Forum', url: 'Link', page: 'Page', label: 'Note', book: 'Book', lesson: 'Lesson', workshop: 'Workshop', choice: 'Poll', feedback: 'Feedback' }

  function fail(e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }

  async function load() {
    try {
      err = ''
      if (cid) {
        d = await api(`courses/${cid}`)
        // Sections with something new start open; the first real section too.
        const o = {}
        d.sections.forEach((s, i) => { o[s.idx] = s.new > 0 || (i === 1 && !d.sections.some((x) => x.new)) })
        open = o
        api(`courses/${cid}/visit`, { method: 'POST' }).catch(() => {})
        if (q) runSearch(q)
      } else list = await api('courses')
    } catch (e) { fail(e) }
  }
  onMount(() => {
    load()
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => removeEventListener('sb-refresh', r)
  })

  const runSearch = debounce(async (text) => {
    const t = (text || '').trim()
    if (t.length < 2) { results = null; return }
    try {
      const r = await api(`search?q=${encodeURIComponent(t)}${d ? `&course=${d.code}` : ''}`)
      results = r.results
    } catch (e) { fail(e) }
  }, 250)
  $effect(() => { runSearch(q) })

  // Where does a search hit lead?
  function hitLink(h) {
    if (h.kind === 'feed') return `#/feed?q=${encodeURIComponent((h.title || '').slice(0, 40))}`
    if (h.cid && h.cid !== cid) return `#/courses/${h.cid}`
    return null
  }
  function openHit(h) {
    if (h.kind === 'module' && d && (!h.cid || h.cid === cid)) {
      const cm = Number(h.ref.slice(4))
      const sec = d.sections.find((x) => x.modules.some((m) => m.cmid === cm))
      if (sec) {
        open[sec.idx] = true; results = null; q = ''
        tick().then(() => document.getElementById(`m-${cm}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }))
        return
      }
    }
    if (h.kind === 'file') { sheet = { file: { url: h.url?.replace(/^file:/, '') || h.ref.slice(5), name: h.title, size: 0 }, find: q }; return }
    const l = hitLink(h); if (l) location.hash = l
  }

  async function addNote() {
    const text = draft.trim(); if (!text) return
    try {
      await api('notes', { method: 'POST', body: { text, kind, course_code: d.code } })
      draft = ''; await load()
    } catch (e) { toast(e.message) }
  }
  async function toggle(n) {
    try { await api(`notes/${n.id}`, { method: 'POST', body: { done: !n.done } }); n.done = n.done ? 0 : 1 } catch (e) { toast(e.message) }
  }
  async function del(n) {
    try { await api(`notes/${n.id}`, { method: 'DELETE' }); d.notes = d.notes.filter((x) => x.id !== n.id) } catch (e) { toast(e.message) }
  }
  const fileOpen = (m, f) => (sheet = { file: f, link: m.url, find: q })
  const tone = (p) => (p < 75 ? 'danger' : p < 80 ? 'warn' : 'ok')
</script>

{#if !cid}
  <Header title="Courses" sub="Contents, search, what's new" sync={list?.sync} keys={['courses', 'index']} />
  {#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

  <div class="search card">{@html I.search}<input bind:value={q} placeholder="Search all your course material…" aria-label="Search course material" /></div>

  {#if results}
    <div class="section"><h2>Results</h2><span class="count num">{results.length}</span></div>
    <div class="card">
      {#if !results.length}<div class="empty">Nothing found{list?.indexed_state.todo ? ` - ${list.indexed_state.todo} files haven't been read yet, so try again later` : ''}.</div>{/if}
      <ul class="list">
        {#each results as h}
          <li>
            <button class="hit" onclick={() => openHit(h)}>
              <div class="row"><span class="pill muted">{h.kind}</span><span class="code">{h.course}</span></div>
              <div class="ht">{h.title}</div>
              {#if h.snip}<div class="sn">{@html hl(h.snip)}</div>{/if}
            </button>
          </li>
        {/each}
      </ul>
    </div>
  {:else if !list}
    <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
  {:else}
    {#if list.indexed_state.total}
      <p class="faint small idx">
        {list.indexed_state.ok} of {list.indexed_state.total} files are searchable{list.indexed_state.todo ? ` · reading ${list.indexed_state.todo} more in the background (a few every 30 min)` : ''}.
      </p>
    {/if}
    {#if !list.courses.length}
      <div class="card empty">No course contents synced yet. Open the menu → Control panel → “Refresh courses”, or wait for the next sync.</div>
    {/if}
    <div class="grid">
      {#each list.courses as c (c.id)}
        <a class="card course" href={`#/courses/${c.id}`}>
          <div class="row">
            <span class="code">{c.code}</span>
            {#if c.new}<span class="pill info">{c.new} new</span>{/if}
            <span class="grow"></span>
            {#if c.attendance != null}<span class={`pill ${tone(c.attendance)}`}>{Math.round(c.attendance)}%</span>{/if}
          </div>
          <div class="title">{c.title}</div>
          <div class="faint small">{c.modules} items · {c.files} files{c.todos ? ` · ${c.todos} to-do${c.todos > 1 ? 's' : ''}` : ''}</div>
          {#if c.next}<div class="next small"><span class="faint">Next:</span> {c.next.title} <span class="faint">· {rel(c.next.due_at)}</span></div>{/if}
        </a>
      {/each}
    </div>
  {/if}
{:else}
  <header class="top detail">
    <a class="iconbtn" href="#/courses" aria-label="All courses">{@html I.left}</a>
    <div class="grow"><h1 class="ellipsis">{d?.title || 'Course'}</h1><div class="sub">{d?.code || ''}</div></div>
  </header>
  {#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

  {#if !d}
    <div class="skeleton"></div><div class="skeleton"></div>
  {:else}
    {#if d.attendance && d.attendance.total}
      <div class="card pad row att">
        <span class={`pill ${tone(d.attendance.pct)}`}>{Math.round(d.attendance.pct)}%</span>
        <span class="grow muted small">attendance · {d.attendance.present}/{d.attendance.total}</span>
        <span class="small">{d.attendance.must_attend ? `attend next ${d.attendance.must_attend}` : `can skip ${d.attendance.can_skip}`}</span>
      </div>
    {/if}

    <div class="search card">{@html I.search}<input bind:value={q} placeholder={`Search inside ${d.code}…`} aria-label="Search this course" /></div>
    {#if results}
      <div class="card">
        {#if !results.length}<div class="empty">Nothing found in this course.</div>{/if}
        <ul class="list">
          {#each results as h}
            <li><button class="hit" onclick={() => openHit(h)}>
              <div class="row"><span class="pill muted">{h.kind}</span></div>
              <div class="ht">{h.title}</div>
              {#if h.snip}<div class="sn">{@html hl(h.snip)}</div>{/if}
            </button></li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if d.items.length}
      <div class="section"><h2>Due soon</h2><span class="count num">{d.items.length}</span></div>
      <div class="card"><ul class="list">
        {#each d.items as i}
          <li class="row due"><span class="pill muted">{i.type}</span><span class="grow ellipsis">{i.title}</span><span class="faint small">{i.due_at ? rel(i.due_at) : ''}</span></li>
        {/each}
      </ul></div>
    {/if}

    <div class="section"><h2>My notes &amp; to-dos</h2><span class="count num">{d.notes.length}</span></div>
    <div class="card">
      <form class="add row" onsubmit={(e) => { e.preventDefault(); addNote() }}>
        <select class="input" bind:value={kind} aria-label="Type"><option value="todo">To-do</option><option value="note">Note</option></select>
        <input class="input grow" bind:value={draft} placeholder={kind === 'todo' ? 'Revise unit 2, finish lab…' : 'Something to remember…'} maxlength="2000" />
        <button class="btn primary" disabled={!draft.trim()}>Add</button>
      </form>
      {#if d.notes.length}
        <ul class="list">
          {#each d.notes as n (n.id)}
            <li class="row note" class:done={n.done}>
              {#if n.kind === 'todo'}
                <input type="checkbox" checked={!!n.done} onchange={() => toggle(n)} aria-label="Done" />
              {:else}<span class="dot" aria-hidden="true"></span>{/if}
              <span class="grow txt">{n.text}</span>
              <button class="x" aria-label="Delete" onclick={() => del(n)}>{@html I.trash}</button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <div class="section"><h2>Contents</h2><span class="count num">{d.sections.reduce((n, s) => n + s.modules.length, 0)}</span></div>
    {#if !d.sections.length}<div class="card empty">Nothing visible in this course yet.</div>{/if}
    {#each d.sections as s (s.idx)}
      <section class="card sec">
        <button class="sh" onclick={() => (open[s.idx] = !open[s.idx])} aria-expanded={!!open[s.idx]}>
          <span class="chev" class:o={open[s.idx]}>{@html I.right}</span>
          <span class="grow ellipsis">{s.name}</span>
          {#if s.new}<span class="pill info">{s.new} new</span>{/if}
          <span class="faint small num">{s.modules.length}</span>
        </button>
        {#if open[s.idx]}
          <ul class="list">
            {#each s.modules as m (m.cmid)}
              <li class="mod" id={`m-${m.cmid}`}>
                <div class="row top">
                  <span class="pill muted">{MOD[m.modname] || m.modname}</span>
                  <span class="grow mn">{#if m.url}<a href={m.url} target="_blank" rel="noopener noreferrer">{m.name}</a>{:else}{m.name}{/if}</span>
                  {#if m.new}<span class="pill info">New</span>{/if}
                </div>
                {#if m.description}<div class="desc faint small">{m.description}</div>{/if}
                {#each m.dates as dt}<div class="faint small">{(dt.label || '').replace(/:$/, '')}: {dt.timestamp ? day(dt.timestamp) : ''}</div>{/each}
                {#each m.files as f}
                  <button class="file" onclick={() => fileOpen(m, f)}>
                    {@html I.file}<span class="grow ellipsis">{f.name}</span>
                    {#if f.new}<span class="pill info">New</span>{/if}
                    <span class="faint small">{kb(f.size)}</span>
                    <span class="sdot" class:ok={f.state === 'ok'} title={f.state === 'ok' ? 'Searchable' : f.state ? 'Not searchable' : 'Waiting to be read'}></span>
                  </button>
                {/each}
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/each}

    {#if d.feed.length}
      <div class="section"><h2>Latest posts</h2><a href={`#/feed?course=${d.code}`}>All</a></div>
      <div class="card"><ul class="list">
        {#each d.feed as p}<li class="row due"><span class="grow ellipsis">{p.title}</span><span class="faint small">{rel(p.posted_at)}</span></li>{/each}
      </ul></div>
    {/if}
  {/if}
  {#if sheet}<FileSheet file={sheet.file} link={sheet.link} find={sheet.find} onclose={() => (sheet = null)} />{/if}
{/if}

<style>
  .small { font-size: 13px; }
  .search { display: flex; align-items: center; gap: 10px; padding: 0 14px; margin-bottom: 12px; }
  .search :global(svg) { width: 18px; height: 18px; color: var(--muted); flex: none; }
  .search input { flex: 1; min-width: 0; background: none; border: 0; outline: 0; padding: 13px 0; font-size: 15px; color: var(--text); }
  .idx { margin: 0 2px 12px; }
  .grid { display: grid; gap: 10px; }
  @media (min-width: 640px) { .grid { grid-template-columns: 1fr 1fr; } }
  .course { display: block; padding: 14px 16px; text-decoration: none; color: inherit; }
  .course:active { background: var(--surface-2); }
  .title { font-weight: 650; margin: 3px 0 2px; line-height: 1.3; }
  .next { margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--line); }
  .hit { display: block; width: 100%; text-align: left; background: none; border: 0; padding: 12px 16px; }
  .ht { font-weight: 600; margin-top: 4px; }
  .sn { color: var(--muted); font-size: 13px; margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .sn :global(mark) { background: color-mix(in srgb, var(--accent) 35%, transparent); color: inherit; border-radius: 3px; padding: 0 1px; }
  .att { margin-bottom: 12px; }
  .due { padding: 10px 16px; }
  .add { padding: 10px 12px; gap: 8px; border-bottom: 1px solid var(--line); }
  .add select { flex: none; }
  .note { padding: 10px 16px; align-items: flex-start; }
  .note.done .txt { text-decoration: line-through; color: var(--faint); }
  .note input[type=checkbox] { margin-top: 3px; accent-color: var(--accent); width: 18px; height: 18px; flex: none; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); margin: 7px 5px 0; flex: none; }
  .txt { white-space: pre-wrap; word-break: break-word; }
  .x { background: none; border: 0; color: var(--faint); padding: 2px; }
  .x :global(svg) { width: 16px; height: 16px; }
  .sec { margin-bottom: 10px; overflow: hidden; }
  .sh { display: flex; align-items: center; gap: 8px; width: 100%; background: none; border: 0; padding: 13px 16px; font-weight: 650; text-align: left; }
  .chev { display: inline-flex; transition: transform .15s; }
  .chev.o { transform: rotate(90deg); }
  .chev :global(svg) { width: 16px; height: 16px; color: var(--muted); }
  .sec .list { border-top: 1px solid var(--line); }
  .mod { padding: 11px 16px; }
  .mn { font-weight: 550; line-height: 1.3; }
  .mn a { color: inherit; text-decoration: none; }
  .mn a:hover { text-decoration: underline; }
  .desc { margin: 4px 0 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .file { display: flex; align-items: center; gap: 8px; width: 100%; margin-top: 6px; min-height: 44px; padding: 8px 12px; border: 0; background: var(--fill); border-radius: 10px; text-align: left; font-size: 15px; }
  .file :global(svg) { width: 16px; height: 16px; color: var(--muted); flex: none; }
  .sdot { width: 8px; height: 8px; border-radius: 50%; background: var(--line); flex: none; }
  .sdot.ok { background: var(--ok); }
</style>
