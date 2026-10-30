<script lang="ts">
  import { Gantt, Willow } from '@svar-ui/svelte-gantt';
  import AppGanttRow from './AppGanttRow.svelte';
  import type { AppDocument } from './apps';
  import { timelineData, ganttLayout } from './timeline';
  let {
    rows,
    group,
    start,
    end,
    dependencies,
    onopen,
    disabled,
    onchange
  }: {
    rows: AppDocument[];
    group?: string;
    start: string;
    end: string;
    dependencies?: string;
    onopen: (path: string) => void;
    disabled: boolean;
    onchange: (
      doc: AppDocument,
      from: string,
      through: string
    ) => Promise<void>;
  } = $props();
  const data = $derived(timelineData(rows, group, start, end));
  const layout = $derived(ganttLayout(data, dependencies));
  const tasks = $derived(
    layout.rows.map((row, rowIndex) => ({
      id: row.id,
      text: row.name,
      start: new Date(Math.min(...data.items.map((item) => +item.start))),
      end: new Date(Math.max(...data.items.map((item) => +item.end))),
      type: 'resource',
      layout,
      rowIndex,
      onopen,
      onchange,
      disabled:
        disabled ||
        !start.startsWith('/') ||
        !end.startsWith('/') ||
        start === end
    }))
  );
  const rangeStart = $derived.by(() => {
    const date = new Date(tasks[0]?.start ?? 0);
    date.setDate(date.getDate() - 1);
    return date;
  });
  const rangeEnd = $derived.by(() => {
    const date = new Date(tasks[0]?.end ?? 0);
    date.setDate(date.getDate() + 1);
    return date;
  });
  let width = $state(800);
  let cellWidth = $state(80);
  const gridWidth = $derived(width < 500 ? 120 : 180);
  function fit() {
    cellWidth = Math.max(
      12,
      Math.min(120, (width - gridWidth) / (layout.to - layout.from + 2))
    );
  }
</script>

<div class="timeline-controls">
  <span class="muted"
    >{group
      ? 'Grouped tasks · overlapping tasks are stacked'
      : 'One row per task'}</span
  ><button onclick={fit}>Fit tasks</button>
</div>
<div
  class="collection-timeline"
  role="region"
  aria-label="Collection schedule"
  bind:clientWidth={width}
  style:height={`${Math.max(240, layout.rows.length * layout.cellHeight + 110)}px`}
>
  {#if tasks.length}
    <Willow
      ><Gantt
        compact={false}
        {tasks}
        {gridWidth}
        start={rangeStart}
        end={rangeEnd}
        autoScale={false}
        readonly
        taskTypes={[{ id: 'resource', label: 'Resource' }]}
        cellHeight={layout.cellHeight}
        {cellWidth}
        columns={[
          { id: 'text', header: group ? 'Resource' : 'Task', width: gridWidth }
        ]}
        scales={[
          { unit: 'month', step: 1, format: '%F %Y' },
          { unit: 'day', step: 1, format: '%d' }
        ]}
        taskTemplate={AppGanttRow}
      /></Willow
    >
  {:else}<p>No scheduled tasks.</p>{/if}
</div>
{#if data.unscheduled.length}<h3>Unscheduled or invalid dates</h3>
  <ul>
    {#each data.unscheduled as doc}<li>
        <button onclick={() => onopen(doc.path)}>{doc.title}</button>
      </li>{/each}
  </ul>{/if}
{#if layout.warnings.length}<ul class="muted">
    {#each layout.warnings as warning}<li>{warning}</li>{/each}
  </ul>{/if}
<p class="muted">
  Drag tasks to move them; drag either edge to resize. Arrow keys adjust by one
  day (Shift: one week). Changes are staged locally. Date ranges include their
  last day.
</p>

<style>
  .timeline-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
  }
  .collection-timeline {
    min-width: 0;
    border: 1px solid #dce3d8;
  }
  .collection-timeline :global(.wx-willow-theme) {
    height: 100%;
  }
  .collection-timeline :global(.wx-bar.resource) {
    background: transparent;
    border: none;
    box-shadow: none;
    cursor: default;
  }
  .collection-timeline :global(.resource .wx-progress-wrapper) {
    display: none;
  }
</style>
