<script>
  import { onMount, tick } from 'svelte'
  import { api, hl, debounce, rel, short } from './lib.js'
  import { I } from './icons.js'

  let { onclose } = $props()
  let q = $state('')
  let data = $state({ courses: [], items: [] })
  let hits = $state([])
  let sel = $state(0)
  let input
  let seq = 0

  const PAGES = [
    ['today', 'Today'], ['deadlines', 'Deadlines'], ['attendance', 'Attendance'], ['timetable', 'Timetable'],
    ['grades', 'Grades'], ['courses', 'Courses'], ['planner', 'Planner'], ['feed', 'Feed'], ['ask', 'Ask'], ['control', 'Control panel'],
  ]
  const KIND = { module: 'Activity', file: 'File', feed: 'Post', note: 'Note' }

  onMount(async () => {
    await tick(); input?.focus()
    try { data = await api('palette') } catch { /* palette still works for pages */ }
  })

  const lookup = debounce(async (text) => {
    const mine = ++seq
    try { const r = await api(`search?q=${encodeURIComponent(text)}`); if (mine === seq) hits = r.results } catch { hits = [] }
  }, 220)
  $effect(() => {
    const t = q.trim()
    sel = 0
    if (t.length >= 2) lookup(t); else { seq++; hits = [] }
  })

  const has = (s, t) => (s || '').toLowerCase().includes(t)
  const rows = $derived.by(() => {
    const t = q.trim().toLowerCase()
    const out = []
    const pages = PAGES.filter(([id, name]) => !t || has(name, t) || has(id, t))
    pages.slice(0, t ? 4 : 10).forEach(([id, name]) => out.push({ k: 'Page', title: name, go: `#/${id}` }))
    data.courses.filter((c) => !t || has(c.title, t) || has(c.code, t)).slice(0, 6)
      .forEach((c) => out.push({ k: 'Course', title: c.title, sub: c.code, go: `#/courses/${c.id}` }))
    data.items.filter((i) => t && (has(i.title, t) || has(i.course_code, t))).slice(0, 5)
      .forEach((i) => out.push({ k: 'Due', title: i.title, sub: `${i.course_code || ''}${i.due_at ? ` · ${rel(i.due_at)}` : ''}`, go: '#/deadlines' }))
    if (!t) data.items.filter((i) => i.due_at).slice(0, 4)
      .forEach((i) => out.push({ k: 'Due', title: i.title, sub: `${i.course_code || ''} · ${rel(i.due_at)}`, go: '#/deadlines' }))
    hits.forEach((h) => out.push({
      k: KIND[h.kind] || h.kind, title: h.title || '(untitled)', sub: h.course, snip: h.snip,
      go: h.kind === 'feed' ? `#/feed?q=${encodeURIComponent(h.title.slice(0, 40))}`
        : h.cid ? `#/courses/${h.cid}?q=${encodeURIComponent(q.trim())}`
        : (data.courses.find((c) => c.code === h.course) ? `#/courses/${data.courses.find((c) => c.code === h.course).id}` : '#/courses'),
    }))
    if (t.length >= 2) out.push({ k: 'Ask', title: `Ask Study Buddy: “${q.trim()}”`, go: `#/ask?q=${encodeURIComponent(q.trim())}` })
    return out
  })

  function pick(r) { location.hash = r.go; onclose() }
  function key(e) {
    if (e.key === 'Escape') { e.preventDefault(); onclose() }
    else if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(rows.length - 1, sel + 1); scroll() }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); scroll() }
    else if (e.key === 'Enter' && rows[sel]) { e.preventDefault(); pick(rows[sel]) }
  }
  async function scroll() { await tick(); document.querySelector('.pal li.on')?.scrollIntoView({ block: 'nearest' }) }
</script>

<button class="sheet-scrim" aria-label="Close search" onclick={onclose}></button>
<div class="pal card glass" role="dialog" aria-modal="true" aria-label="Search">
  <div class="inp">
    {@html I.search}
    <input bind:this={input} bind:value={q} onkeydown={key} placeholder="Jump to a course, deadline, page — or search notes &amp; slides" autocomplete="off" spellcheck="false" enterkeyhint="go" />
    <kbd>esc</kbd>
  </div>
  <ul class="list" role="listbox">
    {#each rows as r, i}
      <li class:on={i === sel} role="option" aria-selected={i === sel}>
        <button onclick={() => pick(r)} onmousemove={() => (sel = i)}>
          <span class={`k k-${r.k}`}>{r.k}</span>
          <span class="grow">
            <span class="t ellipsis">{r.title}</span>
            {#if r.snip}<span class="s snip">{@html hl(r.snip)}</span>{:else if r.sub}<span class="s faint ellipsis">{r.sub}</span>{/if}
          </span>
        </button>
      </li>
    {/each}
    {#if !rows.length}<li class="none faint">Nothing matches.</li>{/if}
  </ul>
</div>

<style>
  .pal { position: fixed; z-index: 50; left: 50%; transform: translateX(-50%); top: calc(env(safe-area-inset-top) + 10vh); width: min(620px, calc(100% - 24px)); max-height: 72dvh; display: flex; flex-direction: column; overflow: hidden; border-radius: 26px !important; }
  .inp { display: flex; align-items: center; gap: 10px; padding: 14px 16px; border-bottom: .5px solid var(--line); }
  .inp :global(svg) { width: 18px; height: 18px; color: var(--muted); flex: none; }
  .inp input { flex: 1; min-width: 0; background: none; border: 0; outline: 0; font-size: 20px; letter-spacing: .019em; color: var(--text); }
  kbd { font-size: 11px; color: var(--faint); border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; }
  ul { overflow: auto; }
  li button { display: flex; gap: 10px; align-items: center; width: 100%; text-align: left; background: none; border: 0; padding: 10px 14px; }
  li button { min-height: 44px; border-radius: 10px; }
  ul { padding: 6px; margin: 0; list-style: none; }
  li.on button { background: var(--accent); color: var(--accent-ink); }
  li.on button .k, li.on button .snip, li.on button .faint { color: inherit; opacity: .85; }
  .k { flex: none; width: 58px; font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
  .k-Course, .k-Ask { color: var(--accent); }
  .k-Due { color: var(--warn); }
  .t { display: block; font-size: 14.5px; }
  .s { display: block; font-size: 12.5px; }
  .snip { color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .snip :global(mark) { background: color-mix(in srgb, var(--accent) 35%, transparent); color: inherit; border-radius: 3px; padding: 0 1px; }
  .none { padding: 16px; text-align: center; }
</style>
