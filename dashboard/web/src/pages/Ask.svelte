<script>
  import { onMount, tick } from 'svelte'
  import { api, AuthError, md, hashQuery } from '../lib.js'
  import { I } from '../icons.js'
  import Header from '../Header.svelte'

  const KEY = 'sb-ask'
  let msgs = $state([])
  let input = $state('')
  let busy = $state(false)
  let usage = $state(null)
  let box

  const IDEAS = ["What's due this week?", 'Can I skip tomorrow’s classes?', 'Where did we cover the pumping lemma?', 'Summarise the mid-sem syllabus for my hardest course']

  onMount(() => {
    try { msgs = JSON.parse(sessionStorage.getItem(KEY) || '[]') } catch { msgs = [] }
    const q = hashQuery().get('q')
    if (q) { history.replaceState(null, '', '#/ask'); send(q) }
    api('control').then((c) => (usage = c.ai)).catch(() => {})
  })
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(msgs.slice(-30))) } catch { /* ignore */ } }
  const down = async () => { await tick(); window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }) }

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || busy) return
    input = ''
    const history = msgs.filter((m) => !m.error).slice(-6).map((m) => ({ role: m.role, content: m.text }))
    msgs = [...msgs, { role: 'user', text: q }]
    busy = true; down()
    try {
      const r = await api('ask', { method: 'POST', body: { q, history } })
      if (r.error) msgs = [...msgs, { role: 'assistant', text: r.error, error: true }]
      else msgs = [...msgs, { role: 'assistant', text: r.answer, tools: r.tools }]
      usage = { ...(usage || {}), used: r.used, limit: r.limit, configured: true }
    } catch (e) {
      if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth'))
      else msgs = [...msgs, { role: 'assistant', text: e.message, error: true }]
    }
    busy = false; save(); down()
  }
  function clear() { msgs = []; save() }
  const TOOL = { search_course_content: 'your notes & slides', get_my_plan: 'your plan', moodle_read_file: 'a course file', mycamu_get_attendance: 'attendance', mycamu_get_timetable: 'timetable', moodle_get_upcoming_deadlines: 'deadlines', moodle_get_grades: 'grades' }
  const label = (t) => TOOL[t] || t.replace(/^(moodle|mycamu)_(get_)?/, '').replace(/_/g, ' ')
  function onkey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }
</script>

<Header title="Ask" sub={usage?.configured ? `${usage.used ?? 0}/${usage.limit} questions today` : 'Chat with your LMS'}>
  {#if msgs.length}<button class="btn sm" onclick={clear}>Clear</button>{/if}
</Header>

<div class="chat" bind:this={box}>
  {#if !msgs.length}
    <div class="card hello">
      <b>Ask about deadlines, attendance, grades — or what's inside your slides.</b>
      <p class="muted small">Uses the same free AI as the Telegram bot, with your LMS and the search index as tools. It only states what it finds.</p>
      <div class="ideas">{#each IDEAS as i}<button class="chip" onclick={() => send(i)}>{i}</button>{/each}</div>
    </div>
  {/if}
  {#each msgs as m, i (i)}
    <div class={`msg ${m.role}`} class:error={m.error}>
      {#if m.role === 'user'}{m.text}{:else}
        <div class="md">{@html md(m.text)}</div>
        {#if m.tools?.length}<div class="tools faint small">Looked at: {m.tools.map(label).join(', ')}</div>{/if}
      {/if}
    </div>
  {/each}
  {#if busy}<div class="msg assistant"><span class="dots"><i></i><i></i><i></i></span></div>{/if}
</div>

<form class="composer" onsubmit={(e) => { e.preventDefault(); send() }}>
  <textarea bind:value={input} onkeydown={onkey} rows="1" placeholder="Ask anything…" aria-label="Question" maxlength="1500"></textarea>
  <button class="send" disabled={busy || !input.trim()} aria-label="Send">{@html I.send}</button>
</form>

<style>
  .small { font-size: 13px; }
  .sm { padding: 6px 12px; font-size: 13px; }
  .chat { display: flex; flex-direction: column; gap: 10px; padding-bottom: 76px; }
  .hello { padding: 16px; }
  .hello p { margin: 6px 0 12px; }
  .ideas { display: flex; flex-wrap: wrap; gap: 8px; }
  .ideas .chip { white-space: normal; text-align: left; }
  .msg { max-width: 92%; padding: 10px 14px; border-radius: 16px; line-height: 1.5; word-break: break-word; }
  .msg.user { align-self: flex-end; background: var(--accent); color: var(--accent-ink); border-bottom-right-radius: 5px; white-space: pre-wrap; }
  .msg.assistant { align-self: flex-start; background: var(--surface); border: 0; border-bottom-left-radius: 6px; }
  .msg.error { border-color: color-mix(in srgb, var(--warn) 50%, var(--line)); background: var(--warn-bg); }
  .md :global(p) { margin: 0 0 6px; }
  .md :global(p:last-child) { margin-bottom: 0; }
  .md :global(ul) { margin: 4px 0 6px; padding-left: 20px; }
  .md :global(code) { background: var(--surface-2); padding: 1px 5px; border-radius: 5px; font-size: .9em; }
  .md :global(a) { color: var(--accent); }
  .tools { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--line); }
  .dots { display: inline-flex; gap: 4px; padding: 4px 0; }
  .dots i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: b 1.2s infinite; }
  .dots i:nth-child(2) { animation-delay: .15s; } .dots i:nth-child(3) { animation-delay: .3s; }
  @keyframes b { 0%, 60%, 100% { opacity: .25; transform: none; } 30% { opacity: 1; transform: translateY(-3px); } }
  .composer { position: fixed; left: 0; right: 0; bottom: calc(var(--nav-h) + env(safe-area-inset-bottom)); z-index: 9; max-width: 760px; margin: 0 auto; display: flex; gap: 8px; padding: 10px 16px; background: color-mix(in srgb, var(--bg) 92%, transparent); backdrop-filter: blur(12px); border-top: 1px solid var(--line); }
  .composer textarea { flex: 1; resize: none; background: var(--surface); border: 0; border-radius: 20px; padding: 10px 16px; min-height: 40px; font-size: 16px; max-height: 120px; color: var(--text); font-family: inherit; }
  .send { width: 44px; height: 44px; border-radius: 50%; border: 0; background: var(--accent); color: var(--accent-ink); display: grid; place-items: center; flex: none; align-self: flex-end; }
  .send:disabled { opacity: .45; }
  .send :global(svg) { width: 20px; height: 20px; }
</style>
