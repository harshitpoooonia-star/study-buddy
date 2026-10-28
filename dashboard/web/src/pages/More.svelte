<script>
  import { I } from '../icons.js'
  import Header from '../Header.svelte'
  import { EXT_DASH } from '../lib.js'
  const LINKS = [
    ['timetable', 'Timetable', 'Classes and free slots', I.timetable],
    ['grades', 'Grades', 'SGPA / CGPA estimate', I.grades],
    ['planner', 'Planner', 'Workload heatmap and to-dos', I.planner],
    ['feed', 'Feed', 'Announcements and notifications', I.feed],
    ['ask', 'Ask', 'Chat with your LMS and notes', I.chat],
    ['control', 'Control panel', 'Sync health, search index, bot', I.sliders],
    ...(EXT_DASH ? [['ext', 'External dashboard', 'Deadlines, subjects, attendance elsewhere', I.ext, EXT_DASH]] : []),
  ]
  const openPalette = () => dispatchEvent(new CustomEvent('sb-palette'))
</script>

<Header title="More" sub="Everything else" />
<button class="card srch" onclick={openPalette}>{@html I.search}<span class="grow">Search or jump to anything</span><kbd>Ctrl K</kbd></button>
<div class="grid">
  {#each LINKS as [id, name, sub, icon, url]}
    <a class="card tile" href={url || `#/${id}`} target={url ? '_blank' : undefined} rel={url ? 'noopener' : undefined}>{@html icon}<div><b>{name}</b><div class="faint small">{sub}</div></div></a>
  {/each}
</div>

<style>
  .small { font-size: 13px; }
  .srch { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 40px; padding: 0 10px; margin-bottom: 16px; color: var(--muted); text-align: left; border: 0; background: var(--fill); border-radius: 12px; font-size: 17px; }
  .srch :global(svg) { width: 18px; height: 18px; }
  kbd { font-size: 11px; color: var(--faint); border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; }
  .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 160px), 1fr)); }
  .tile { display: flex; flex-direction: column; gap: 12px; padding: 16px; text-decoration: none; color: inherit; min-height: 112px; transition: transform .15s; }
  .tile b { font-weight: 600; font-size: 17px; letter-spacing: -.022em; }
  .tile :global(svg) { width: 26px; height: 26px; color: var(--accent); }
  .tile:active { transform: scale(.97); }
</style>
