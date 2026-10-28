<script>
  import { api, day, time, rel, statusOf, KIND, toast } from './lib.js'
  import { I } from './icons.js'

  let { it, now, onchange, compact = false } = $props()
  let open = $state(false)

  const st = $derived(statusOf(it))
  const urgent = $derived(!it.done && it.due_at && it.due_at - now < 24 * 3600 && it.due_at > now)

  async function mark(state, extra = {}) {
    open = false
    try {
      await api('marks', { method: 'POST', body: { item_id: it.id, state, ...extra } })
      toast(state === 'clear' ? 'Cleared' : state === 'snoozed' ? 'Snoozed for a day' : `Marked ${state}`)
      onchange?.()
    } catch (e) { toast(e.message) }
  }
  async function remove() {
    open = false
    const path = it.exam_id ? `exams/${it.exam_id}` : `entries/${it.manual_id}`
    try { await api(path, { method: 'DELETE' }); toast('Deleted'); onchange?.() } catch (e) { toast(e.message) }
  }
</script>

<li class="item" class:done={it.done} class:urgent>
  <div class="rail" data-kind={it.type}></div>
  <div class="grow">
    <div class="meta">
      {#if it.course_code}<span class="code">{it.course_code}</span>{/if}
      <span class="faint kind">{KIND[it.type] || it.type}</span>
      {#if it.mark === 'started'}<span class="pill info">Started</span>{/if}
    </div>
    <div class="title">
      {#if it.url}<a href={it.url} target="_blank" rel="noopener">{it.title}</a>{:else}{it.title}{/if}
    </div>
    <div class="when">
      {#if it.type === 'quiz' && it.opens_at && it.opens_at > now}
        <span class="num">Opens {day(it.opens_at)} · {time(it.opens_at)}</span>
        <span class="rel num">{rel(it.opens_at, now)}</span>
      {:else if it.due_at}
        <span class="num">{compact ? '' : day(it.due_at) + ' · '}{time(it.due_at)}</span>
        <span class="rel num" class:hot={urgent || st[1] === 'danger'}>{rel(it.due_at, now)}</span>
      {:else}<span class="faint">No due date</span>{/if}
      {#if it.grade}<span class="faint">· {it.grade}{it.max_grade ? ` / ${+it.max_grade}` : ''}</span>{/if}
      {#if it.type === 'exam' && it.detail?.time_known === false}<span class="faint">· time TBC</span>{/if}
      {#if it.type === 'exam' && it.detail?.venue}<span class="faint">· {it.detail.venue}</span>{/if}
      {#if it.type === 'quiz' && it.detail?.time_limit_min}<span class="faint">· {it.detail.time_limit_min} min</span>{/if}
    </div>
  </div>
  <div class="side">
    <span class={`pill ${st[1]}`}>{st[0]}</span>
    <button class="dots" aria-label="Actions" aria-expanded={open} onclick={() => (open = !open)}>{@html I.more}</button>
    {#if open}
      <button class="scrim" aria-label="Close" onclick={() => (open = false)}></button>
      <div class="menu card glass" role="menu">
        {#if it.mark !== 'started' && !it.done}<button role="menuitem" onclick={() => mark('started')}>Mark started</button>{/if}
        {#if it.mark !== 'done'}<button role="menuitem" onclick={() => mark('done')}>Mark done</button>{/if}
        {#if !it.done}<button role="menuitem" onclick={() => mark('snoozed', { hours: 24 })}>Snooze 1 day</button>{/if}
        {#if it.mark}<button role="menuitem" onclick={() => mark('clear')}>Clear my mark</button>{/if}
        {#if it.url}<a role="menuitem" href={it.url} target="_blank" rel="noopener">Open in LMS</a>{/if}
        {#if it.manual_id || it.exam_id}<button role="menuitem" class="del" onclick={remove}>{it.exam_id ? 'Delete exam' : 'Delete entry'}</button>{/if}
      </div>
    {/if}
  </div>
</li>

<style>
  .item { display: flex; gap: 12px; padding: 12px 12px 12px 16px; position: relative; min-height: 44px; }
  .rail { width: 10px; height: 10px; border-radius: 50%; margin-top: 6px; background: var(--faint); flex: none; }
  .rail[data-kind='quiz'] { background: var(--indigo); }
  .rail[data-kind='assign'] { background: var(--accent); }
  .rail[data-kind='exam'] { background: var(--red); border-radius: 2px; transform: rotate(45deg) scale(.85); }
  .done .rail { background: none; box-shadow: inset 0 0 0 2px var(--faint); }
  .done .title { color: var(--muted); }
  .meta { display: flex; gap: 8px; align-items: center; font-size: 12px; }
  .kind { font-size: 12px; }
  .title { font-weight: 600; margin: 2px 0 3px; font-size: 17px; line-height: 22px; letter-spacing: -.022em; word-break: break-word; }
  .title a { text-decoration: none; }
  .title a:hover { text-decoration: underline; }
  .when { font-size: 15px; line-height: 20px; letter-spacing: -.012em; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 8px; }
  .rel.hot { color: var(--danger); font-weight: 600; }
  .side { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; position: relative; flex: none; }
  .dots { border: 0; background: var(--fill); color: var(--accent); width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center; margin: -4px -2px; }
  .dots :global(svg) { width: 18px; height: 18px; display: block; }
  .scrim { position: fixed; inset: 0; background: transparent; border: 0; z-index: 29; }
  .menu { position: absolute; right: 0; top: 60px; z-index: 30; min-width: 220px; padding: 6px 0; transform-origin: top right; animation: popin .22s cubic-bezier(.2, .9, .3, 1.1); }
  @keyframes popin { from { opacity: 0; transform: scale(.92); } }
  .menu button, .menu a { display: flex; align-items: center; width: 100%; min-height: 44px; text-align: left; padding: 0 16px; border: 0; background: none; font-size: 17px; letter-spacing: -.022em; text-decoration: none; position: relative; }
  .menu > * + *::before { content: ''; position: absolute; top: 0; left: 16px; right: 0; height: 1px; background: var(--line); transform: scaleY(.5); }
  .menu button:hover, .menu a:hover { background: var(--fill); }
  @media (min-width: 1024px) { .menu button, .menu a { min-height: 34px; font-size: 14px; } }
  .menu .del { color: var(--danger); }
</style>
