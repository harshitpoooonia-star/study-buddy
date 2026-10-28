<script>
  import { onMount } from 'svelte'
  import { api, AuthError, toast } from '../lib.js'
  import Header from '../Header.svelte'

  let d = $state(null)
  let err = $state('')
  let open = $state({})
  let saveTimer
  let saving = $state(false)

  async function load() {
    try { d = await api('grades'); err = '' }
    catch (e) { if (e instanceof AuthError) dispatchEvent(new CustomEvent('sb-auth')); else err = e.message }
  }
  onMount(() => {
    load()
    const r = () => load()
    addEventListener('sb-refresh', r)
    return () => { removeEventListener('sb-refresh', r); clearTimeout(saveTimer) }
  })

  const GRADES = ['O', 'A+', 'A', 'B+', 'B', 'C', 'P', 'F']

  // Live numbers while editing (the server recomputes the same on save).
  const live = $derived.by(() => {
    if (!d) return null
    let pts = 0, crs = 0
    for (const c of d.courses) {
      const cr = parseFloat(c.credits)
      if (cr > 0 && c.expected in d.scale) { pts += cr * d.scale[c.expected]; crs += cr }
    }
    let pp = 0, pc = 0
    for (const t of d.past) { const sg = parseFloat(t.sgpa), cr = parseFloat(t.credits); if (sg >= 0 && cr > 0) { pp += sg * cr; pc += cr } }
    return {
      sgpa: crs ? (pts / crs).toFixed(2) : null, crs,
      cgpaPast: pc ? (pp / pc).toFixed(2) : null,
      cgpa: pc + crs ? ((pp + pts) / (pc + crs)).toFixed(2) : null,
      missing: d.courses.filter((c) => !(parseFloat(c.credits) > 0) || !c.expected).length,
    }
  })

  function queueSave() {
    clearTimeout(saveTimer)
    saveTimer = setTimeout(save, 700)
  }
  async function save() {
    saving = true
    try {
      const res = await api('grades/plan', { method: 'POST', body: {
        courses: d.courses.map((c) => ({ code: c.code, credits: c.credits === '' ? null : c.credits, expected: c.expected || null })),
        past: d.past.filter((t) => t.term && t.sgpa !== '' && t.credits !== ''),
      } })
      d.sgpa = res.sgpa; d.cgpa = res.cgpa
    } catch (e) { toast(e.message) }
    saving = false
  }
  function addTerm() { d.past = [...d.past, { term: `Semester ${d.past.length + 1}`, sgpa: '', credits: '' }] }
  function delTerm(i) { d.past = d.past.filter((_, j) => j !== i); queueSave() }
  const pct = (c) => {
    const g = parseFloat(c.total)
    return Number.isFinite(g) && c.total_max ? Math.round((1000 * g) / c.total_max) / 10 : null
  }
</script>

<Header title="Grades" sub="SGPA / CGPA estimate" sync={d?.sync} keys={['grades']} />

{#if err}<div class="banner danger"><b>Couldn't load.</b> {err}</div>{/if}

{#if !d}
  <div class="skeleton" style="height:120px"></div><div class="skeleton"></div>
{:else}
  <section class="card summary">
    <div class="stat">
      <div class="k">This semester</div>
      <div class="v num">{live.sgpa ?? '—'}</div>
      <div class="faint small">SGPA · {live.crs} credits counted</div>
    </div>
    <div class="div"></div>
    <div class="stat">
      <div class="k">Overall</div>
      <div class="v num accent">{live.cgpa ?? '—'}</div>
      <div class="faint small">CGPA{live.cgpaPast ? ` · ${live.cgpaPast} before this sem` : ''}</div>
    </div>
  </section>
  {#if live.missing}
    <p class="faint small hintline">Add credits and the grade you expect for {live.missing} more course{live.missing > 1 ? 's' : ''} to include them. {saving ? 'Saving…' : ''}</p>
  {:else}<p class="faint small hintline">{saving ? 'Saving…' : 'Saved.'}</p>{/if}

  <div class="section"><h2>This semester</h2><span class="count num">{d.courses.length}</span></div>
  <div class="card">
    <ul class="list">
      {#each d.courses as c (c.code)}
        <li class="course">
          <div class="row top">
            <button class="grow name" onclick={() => (open[c.code] = !open[c.code])} aria-expanded={!!open[c.code]}>
              <span class="code">{c.code}</span>
              <span class="ellipsis nm">{c.name}</span>
              {#if c.total != null && c.total !== '-' && c.total !== '—'}
                <span class="faint small num">LMS total {c.total}{c.total_max ? ` / ${c.total_max}` : ''}{pct(c) != null ? ` · ${pct(c)}%` : ''}</span>
              {:else}<span class="faint small">No LMS marks yet</span>{/if}
            </button>
            <label class="cr"><input class="input num" type="number" inputmode="decimal" min="0" max="30" step="0.5" placeholder="cr"
              bind:value={c.credits} oninput={queueSave} aria-label={`${c.code} credits`} /></label>
            <select class="input gsel" bind:value={c.expected} onchange={queueSave} aria-label={`${c.code} expected grade`}>
              <option value={null}>—</option>
              {#each GRADES as g}<option value={g}>{g}</option>{/each}
            </select>
          </div>
          {#if open[c.code]}
            {#if c.items.length}
              <ul class="items">
                {#each c.items as g}<li><span class="ellipsis">{g.item}</span><span class="num">{g.grade}{g.max ? ` / ${g.max}` : ''}</span></li>{/each}
              </ul>
            {:else}<div class="faint small items">No graded items on the LMS yet.</div>{/if}
          {/if}
        </li>
      {/each}
    </ul>
  </div>

  <div class="section"><h2>Past semesters</h2><span class="grow"></span><button class="link" onclick={addTerm}>+ Add</button></div>
  <div class="card">
    {#if !d.past.length}
      <div class="empty">Add your previous SGPAs (from your result cards) to get CGPA. MyCamu's API doesn't return them.</div>
    {:else}
      <ul class="list">
        {#each d.past as t, i}
          <li class="row term">
            <input class="input grow" bind:value={t.term} oninput={queueSave} aria-label="Semester name" />
            <input class="input num sm" type="number" step="0.01" min="0" max="10" placeholder="SGPA" bind:value={t.sgpa} oninput={queueSave} aria-label="SGPA" />
            <input class="input num sm" type="number" step="0.5" min="0" max="60" placeholder="Credits" bind:value={t.credits} oninput={queueSave} aria-label="Credits" />
            <button class="x" aria-label="Remove" onclick={() => delTerm(i)}>×</button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <p class="faint small note">Grade points: {Object.entries(d.scale).map(([g, p]) => `${g} ${p}`).join(' · ')} (10-point scale). Bennett grades relatively, so pick the grade you expect, not a % cut-off. Credits are on your registration slip / course handout.</p>
{/if}

<style>
  .small { font-size: 13px; }
  .summary { display: flex; align-items: stretch; padding: 16px 18px; gap: 16px; }
  .stat { flex: 1; }
  .stat .k { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; }
  .stat .v { font-size: 36px; font-weight: 750; letter-spacing: -.02em; line-height: 1.15; }
  .stat .v.accent { color: var(--accent); }
  .div { width: 1px; background: var(--line); }
  .hintline { margin: 8px 2px 0; }
  .course { padding: 10px 12px 10px 16px; }
  .top { gap: 8px; }
  .name { display: flex; flex-direction: column; align-items: flex-start; text-align: left; background: none; border: 0; padding: 0; min-width: 0; }
  .nm { font-weight: 600; max-width: 100%; }
  .cr input { width: 58px; text-align: center; }
  .gsel { width: 64px; }
  .items { list-style: none; margin: 8px 0 2px; padding: 8px 0 0; border-top: 1px dashed var(--line); font-size: 13px; color: var(--muted); }
  .items li { display: flex; justify-content: space-between; gap: 12px; padding: 2px 0; }
  .term { padding: 8px 12px; gap: 8px; }
  .term .sm { width: 76px; }
  .x { background: none; border: 0; color: var(--faint); font-size: 22px; line-height: 1; padding: 0 4px; }
  .note { margin-top: 16px; }
</style>
