<script>
  import { onMount } from 'svelte'
  import { api, esc, kb } from './lib.js'

  let { file, link = null, find = '', onclose } = $props()
  let d = $state(null)
  let err = $state('')

  onMount(async () => {
    try { d = await api(`file?url=${encodeURIComponent(file.url)}`) } catch (e) { err = e.message }
  })

  // Highlight the search words inside the preview.
  const body = $derived.by(() => {
    if (!d) return ''
    let t = esc(d.text)
    const words = (find || '').toLowerCase().match(/[\w+#]{2,}/g) || []
    for (const w of words.slice(0, 6)) t = t.replace(new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi'), '<mark>$1</mark>')
    return t
  })
</script>

<button class="sheet-scrim" aria-label="Close" onclick={onclose}></button>
<div class="sheet glass" role="dialog" aria-modal="true" aria-label={file.name}>
  <div class="grab"></div>
  <h3 class="ellipsis">{file.name}</h3>
  <p class="muted small">{kb(file.size)}{d?.live ? ' · read live from the LMS' : d ? ' · indexed text' : ''}{link ? '' : ''}</p>
  {#if err}
    <div class="banner warn"><div>{err}</div></div>
  {:else if !d}
    <div class="skeleton"></div><div class="skeleton"></div>
  {:else if !d.text}
    <div class="banner warn"><div>No readable text in this file (maybe scanned images). Open it on the LMS instead.</div></div>
  {:else}
    <pre class="txt">{@html body}</pre>
    {#if d.truncated}<p class="faint small">Preview shows the first 40,000 characters.</p>{/if}
  {/if}
  <div class="row act">
    {#if link}<a class="btn" href={link} target="_blank" rel="noopener noreferrer">Open on LMS</a>{/if}
    <span class="grow"></span>
    <button class="btn" onclick={onclose}>Close</button>
  </div>
</div>

<style>
  .small { font-size: 13px; margin: 0 0 10px; }
  h3 { max-width: 100%; }
  .txt { white-space: pre-wrap; word-break: break-word; font: 13px/1.5 var(--mono); background: var(--surface); border: 0; border-radius: 10px; padding: 12px; max-height: 52dvh; overflow: auto; margin: 0; }
  .txt :global(mark) { background: color-mix(in srgb, var(--accent) 40%, transparent); color: inherit; border-radius: 3px; }
  .act { margin-top: 12px; }
  a.btn { text-decoration: none; display: inline-block; }
</style>
