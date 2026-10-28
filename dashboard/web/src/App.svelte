<script>
  import { onMount } from 'svelte'
  import { api, AuthError, EXT_DASH } from './lib.js'
  import { I } from './icons.js'
  import Login from './Login.svelte'
  import Today from './pages/Today.svelte'
  import Deadlines from './pages/Deadlines.svelte'
  import Attendance from './pages/Attendance.svelte'
  import Timetable from './pages/Timetable.svelte'
  import Grades from './pages/Grades.svelte'
  import Courses from './pages/Courses.svelte'
  import Planner from './pages/Planner.svelte'
  import Feed from './pages/Feed.svelte'
  import Ask from './pages/Ask.svelte'
  import Control from './pages/Control.svelte'
  import More from './pages/More.svelte'
  import Palette from './Palette.svelte'

  const PAGES = [
    { id: 'today', label: 'Today', icon: I.today, C: Today },
    { id: 'deadlines', label: 'Deadlines', icon: I.deadlines, C: Deadlines },
    { id: 'attendance', label: 'Attendance', icon: I.attendance, C: Attendance },
    { id: 'courses', label: 'Courses', icon: I.book, C: Courses },
    { id: 'more', label: 'More', icon: I.more, C: More },
    { id: 'timetable', label: 'Timetable', icon: I.timetable, C: Timetable, hidden: true },
    { id: 'grades', label: 'Grades', icon: I.grades, C: Grades, hidden: true },
    { id: 'planner', label: 'Planner', icon: I.planner, C: Planner, hidden: true },
    { id: 'feed', label: 'Feed', icon: I.feed, C: Feed, hidden: true },
    { id: 'ask', label: 'Ask', icon: I.chat, C: Ask, hidden: true },
    { id: 'control', label: 'Control panel', icon: I.sliders, C: Control, hidden: true },
  ]
  const MORE = ['timetable', 'grades', 'planner', 'feed', 'ask', 'control']

  let route = $state(readRoute().id)
  let arg = $state(readRoute().arg)
  let palette = $state(false)
  let authed = $state(null) // null = checking
  let toastMsg = $state('')
  let overdue = $state(0)
  let toastTimer

  function readRoute() {
    const [id, a = ''] = location.hash.replace(/^#\/?/, '').split('?')[0].split('/')
    return PAGES.some((p) => p.id === id) ? { id, arg: a } : { id: 'today', arg: '' }
  }

  const page = $derived(PAGES.find((p) => p.id === route))
  const TABS = PAGES.filter((x) => !x.hidden)
  const tabIdx = $derived(MORE.includes(route) ? TABS.length - 1 : Math.max(0, TABS.findIndex((t) => t.id === route)))

  onMount(() => {
    const onHash = () => { const r = readRoute(); route = r.id; arg = r.arg; window.scrollTo(0, 0) }
    const onToast = (e) => { toastMsg = e.detail; clearTimeout(toastTimer); toastTimer = setTimeout(() => (toastMsg = ''), 3200) }
    const onAuth = () => (authed = false)
    const onOverdue = (e) => (overdue = e.detail)
    const onPal = () => (palette = true)
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette = !palette }
      else if (e.key === '/' && !typing && !palette) { e.preventDefault(); palette = true }
    }
    addEventListener('sb-palette', onPal)
    addEventListener('keydown', onKey)
    addEventListener('hashchange', onHash)
    addEventListener('sb-toast', onToast)
    addEventListener('sb-auth', onAuth)
    addEventListener('sb-overdue', onOverdue)
    api('auth/config').then((c) => (authed = c.logged_in)).catch((e) => (authed = e instanceof AuthError ? false : true))
    return () => {
      removeEventListener('hashchange', onHash); removeEventListener('sb-toast', onToast)
      removeEventListener('sb-palette', onPal); removeEventListener('keydown', onKey)
      removeEventListener('sb-auth', onAuth); removeEventListener('sb-overdue', onOverdue)
    }
  })
</script>

{#if authed === false}
  <Login />
{:else if authed}
  <main class="shell">
    {#key `${route}/${arg}`}
      <page.C {arg} />
    {/key}
  </main>
  <div class="edge-top" aria-hidden="true"></div>
  <nav class="nav glass" aria-label="Main">
    <div class="nav-in" style={`--i:${tabIdx}`}>
      <span class="lens" aria-hidden="true"></span>
      <div class="brand"><img src="/dash/icon-192.png" alt="" />Study Buddy</div>
      {#each PAGES.filter((x) => !x.hidden) as p}
        {@const on = route === p.id || (p.id === 'more' && MORE.includes(route))}
        <a href={`#/${p.id}`} class:on class:tab-only={p.id === 'more'} aria-current={on ? 'page' : undefined}>
          {@html p.icon}
          <span>{p.label}</span>
          {#if p.id === 'deadlines' && overdue > 0}<span class="badge num">{overdue}</span>{/if}
        </a>
      {/each}
      <div class="side-head">Library</div>
      {#each PAGES.filter((x) => MORE.includes(x.id)) as p}
        <a href={`#/${p.id}`} class="side-only" class:on={route === p.id} aria-current={route === p.id ? 'page' : undefined}>
          {@html p.icon}<span>{p.label}</span>
        </a>
      {/each}
      {#if EXT_DASH}<a href={EXT_DASH} class="side-only" target="_blank" rel="noopener">{@html I.ext}<span>External dashboard</span></a>{/if}
    </div>
  </nav>
{/if}

{#if authed && palette}<Palette onclose={() => (palette = false)} />{/if}
{#if toastMsg}<div class="toast glass" role="status">{toastMsg}</div>{/if}
