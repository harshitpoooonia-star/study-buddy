<script>
  import { onMount } from 'svelte'
  import { api, AuthError, toast, rel, hashQuery, debounce } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'

  const init = hashQuery()
  let items = $state(null)
  let unread = $state({})
  let more = $state(false)
  let err = $state('')
  let source = $state(init.get('source') || '')
  let onlyUnread = $state(false)
  let q = $state(init.get('q') || '')
  let course = $state(init.get('course') || '')
  let sync = $state(null)
  let openId = $state('')

  const SRC = { moodle: 'LMS', course: 'Course', mycamu: 'MyCamu' }
  const totalUnread = $derived(Object.values(unread).reduce((a, b) => a + b, 0))

  function params(before = 0) {
    const p = new URLSearchParams()
    if (source) p.set('source', source)
    if (course) p.set('course', course)
    if (onlyUnread) p.set('unread', '1')
    if (q.trim()) p.set('q', q.trim())
    if (before) p.set('before', before)
    return p.toString()
  }
  async function load(append = false) {
    try {
      const r = await api(`feed?${params(append && items?.length ? items[items.length - 1].posted_at : 0)}`)
      items = append ? [...items, ...r.items] : r.items
      unread = r.unread; more = r.more; sync = r.sync; err = ''
    } catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  const reload = debounce(() => load(), 250)
  $effect(() => { source; onlyUnread; q; course; reload() })
  onMount(() => {
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => removeEventListener('sb-refresh', r)
  })

  async function expand(it) {
    openId = openId === it.id ? '' : it.id
    if (openId && !it.read) {
      it.read = 1; unread = { ...unread, [it.source]: Math.max(0, (unread[it.source] || 1) - 1) }
      api('feed/read', { method: 'POST', body: { ids: [it.id] } }).catch(() => {})
    }
  }
  async function markAll() {
    try {
      await api('feed/read', { method: 'POST', body: { all: true, source: source || undefined } })
      items?.forEach((i) => { if (!source || i.source === source) i.read = 1 })
      await load(); toast('Marked as read')
    } catch (e) { toast(e.message) }
  }
</script>

<Header title="Feed" sub={totalUnread ? `${totalUnread} unread` : 'All caught up'} {sync} keys={['feed']} />
{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

<div class="search card">{@html I.search}<input bind:value={q} placeholder="Search posts…" aria-label="Search posts" /></div>
<div class="chips fl">
  <button class="chip" class:on={!source && !onlyUnread && !course} onclick={() => { source = ''; onlyUnread = false; course = '' }}>All</button>
  <button class="chip" class:on={onlyUnread} onclick={() => (onlyUnread = !onlyUnread)}>Unread{totalUnread ? ` ${totalUnread}` : ''}</button>
  {#each Object.entries(SRC) as [k, label]}
    <button class="chip" class:on={source === k} onclick={() => (source = source === k ? '' : k)}>{label}{unread[k] ? ` ${unread[k]}` : ''}</button>
  {/each}
  {#if course}<button class="chip on" onclick={() => (course = '')}>{course} ✕</button>{/if}
</div>
<div class="bar"><span class="grow faint small">{items ? `${items.length}${more ? '+' : ''} posts` : ''}</span><button class="link" onclick={markAll}>Mark all read</button></div>

{#if !items}
  <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
{:else if !items.length}
  <div class="card empty">{q || source || onlyUnread || course ? 'Nothing matches those filters.' : 'No posts synced yet. Announcements and notifications appear here after the next sync.'}</div>
{:else}
  <div class="card">
    <ul class="list">
      {#each items as it (it.id)}
        <li class:unread={!it.read}>
          <button class="post" onclick={() => expand(it)} aria-expanded={openId === it.id}>
            <div class="row meta">
              <span class="ud" aria-label={it.read ? 'read' : 'unread'}></span>
              <span class={`pill ${it.source === 'mycamu' ? 'warn' : it.source === 'course' ? 'info' : 'muted'}`}>{SRC[it.source] || it.source}</span>
              {#if it.course_code}<span class="code">{it.course_code}</span>{/if}
              <span class="grow"></span>
              <span class="faint small">{it.posted_at ? rel(it.posted_at) : ''}</span>
            </div>
            <div class="pt">{it.title}</div>
            {#if openId === it.id}
              {#if it.body}<div class="pb">{it.body}</div>{/if}
              <div class="row pa small">
                {#if it.author}<span class="faint">{it.author}</span>{/if}
                <span class="grow"></span>
                {#if it.url}<a href={it.url} target="_blank" rel="noopener noreferrer" onclick={(e) => e.stopPropagation()}>Open {@html I.ext}</a>{/if}
              </div>
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  </div>
  {#if more}<button class="btn lm" onclick={() => load(true)}>Load older</button>{/if}
{/if}

<style>
  .small { font-size: 13px; }
  .search { display: flex; align-items: center; gap: 10px; padding: 0 14px; margin-bottom: 10px; }
  .search :global(svg) { width: 18px; height: 18px; color: var(--muted); flex: none; }
  .search input { flex: 1; min-width: 0; background: none; border: 0; outline: 0; padding: 12px 0; font-size: 15px; color: var(--text); }
  .fl { margin-bottom: 4px; }
  .bar { display: flex; align-items: center; margin: 8px 2px; }
  .bar .link { font-size: 13px; color: var(--accent); background: none; border: 0; padding: 0; }
  .post { display: block; width: 100%; text-align: left; background: none; border: 0; padding: 12px 16px; }
  .meta { gap: 8px; margin-bottom: 4px; }
  .ud { width: 7px; height: 7px; border-radius: 50%; background: transparent; flex: none; }
  li.unread .ud { background: var(--info); }
  li.unread .pt { font-weight: 650; }
  .pt { line-height: 1.35; }
  .pb { margin-top: 8px; white-space: pre-wrap; word-break: break-word; color: var(--muted); font-size: 14px; line-height: 1.5; }
  .pa { margin-top: 8px; }
  .pa a { color: var(--accent); text-decoration: none; display: inline-flex; align-items: center; gap: 4px; }
  .pa :global(svg) { width: 14px; height: 14px; }
  .lm { display: block; margin: 12px auto 0; }
</style>
