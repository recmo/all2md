<script lang="ts">
  import AppGanttBar from './AppGanttBar.svelte';
  import type { ITask } from '@svar-ui/svelte-gantt';
  import type { ganttLayout } from './timeline';
  type Layout = ReturnType<typeof ganttLayout>;
  let { data }: { data: ITask } = $props();
  const layout: Layout = $derived(data.layout);
  const row = $derived(layout.rows[data.rowIndex]);
  const unit = $derived(data.$w / (layout.to - layout.from));
  function arrow(source: string, target: string) {
    const a = layout.positions.get(source)!, b = layout.positions.get(target)!;
    const x1 = (a.to - layout.from) * unit, x2 = (b.from - layout.from) * unit;
    const y1 = a.lane * 36 + 15, y2 = (b.row - a.row) * layout.cellHeight + b.lane * 36 + 15;
    // Route backwards links through the gap between lanes, away from bar text.
    const bend = y2 === y1 ? y1 + 18 : y1 + (y2 > y1 ? 18 : -18);
    return `M${x1},${y1} H${x1 + 8} V${bend} H${x2 - 8} V${y2} H${x2}`;
  }
</script>
<svg class="dependencies" aria-label="Task dependencies">
  {#each layout.links.filter(link => row.bars.some(bar => bar.id === link.source)) as link}
    {@const target = layout.positions.get(link.target)!}
    {@const x = (target.from - layout.from) * unit}
    {@const y = (target.row - data.rowIndex) * layout.cellHeight + target.lane * 36 + 15}
    <g data-source={link.source} data-target={link.target}>
      <title>{link.source} → {link.target}</title>
      <path d={arrow(link.source, link.target)} />
      <path class="head" d={`M${x-5},${y-4} L${x},${y} L${x-5},${y+4}`} />
    </g>
  {/each}
</svg>
{#each row.bars as bar}
  <AppGanttBar {bar} origin={layout.from} {unit} disabled={data.disabled} onopen={data.onopen} onchange={data.onchange} />
{/each}
<style>
  .dependencies { position: absolute; left: 0; top: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; z-index: 1; }
  path { fill: none; stroke: #526c80; stroke-width: 1.5; }
</style>
