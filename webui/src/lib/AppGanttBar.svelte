<script lang="ts">
  import { shiftRange, dateFromDay, type GanttGesture, type ganttLayout } from './timeline';
  import type { AppDocument } from './apps';
  type Bar = ReturnType<typeof ganttLayout>['rows'][number]['bars'][number];
  let { bar, origin, unit, disabled, onopen, onchange }: {bar: Bar; origin: number; unit: number; disabled: boolean; onopen: (path: string) => void; onchange: (doc: AppDocument, from: string, through: string) => Promise<void>} = $props();
  let drag = $state<{x: number; from: number; to: number; unit: number; mode: GanttGesture; days: number; moved: boolean; doc: AppDocument} | null>(null);
  let suppressClick = false;
  const range = $derived(drag ? shiftRange(drag.from, drag.to, drag.days, drag.mode) : bar);
  const description = $derived(`${bar.doc.title}: ${dateFromDay(range.from)} – ${dateFromDay(range.to-1)}`);
  function begin(event: PointerEvent, mode: GanttGesture) {
    event.stopPropagation();
    if (disabled || event.button !== 0 || !event.isPrimary || !(unit > 0)) return;
    suppressClick = false;
    drag = {x:event.clientX, from:bar.from, to:bar.to, unit, mode, days:0, moved:false, doc:bar.doc};
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }
  function motion(event: PointerEvent) {
    if (!drag) return;
    event.stopPropagation();
    drag.days = Math.round((event.clientX - drag.x) / drag.unit);
    drag.moved ||= Math.abs(event.clientX - drag.x) > 4;
  }
  function finish(event: PointerEvent) {
    if (!drag) return;
    event.stopPropagation();
    const old = drag, next = shiftRange(old.from, old.to, old.days, old.mode);
    suppressClick = old.moved;
    drag = null;
    if (!disabled && (next.from !== old.from || next.to !== old.to)) void onchange(old.doc, dateFromDay(next.from), dateFromDay(next.to-1));
  }
  function cancel() { if (drag) { suppressClick = true; drag = null; } }
  function keyboard(event: KeyboardEvent, mode: GanttGesture) {
    if (event.key === 'Escape') { cancel(); event.stopPropagation(); return; }
    if (disabled || !['ArrowLeft','ArrowRight'].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const days = (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 7 : 1);
    const next = shiftRange(bar.from, bar.to, days, mode);
    if (next.from !== bar.from || next.to !== bar.to) void onchange(bar.doc, dateFromDay(next.from), dateFromDay(next.to-1));
  }
</script>
<div class="task-frame" class:dragging={!!drag} style:left={`${(range.from-origin)*unit}px`} style:width={`${(range.to-range.from)*unit}px`} style:top={`${bar.lane*36}px`} title={description}>
  <button class="task" aria-label={bar.doc.title} aria-disabled={disabled} onpointerdown={e => begin(e,'move')} onpointermove={motion} onpointerup={finish} onpointercancel={cancel} onlostpointercapture={cancel} onkeydown={e => keyboard(e,'move')}
    onclick={e => { e.stopPropagation(); if (suppressClick) { suppressClick = false; return; } onopen(bar.id); }}>{bar.doc.title}</button>
  {#each ['start', 'end'] as edge}
    <button class="edge {edge}" disabled={disabled} aria-label={`Resize ${edge} of ${bar.doc.title}`} onpointerdown={e => begin(e,edge as GanttGesture)} onpointermove={motion} onpointerup={finish} onpointercancel={cancel} onlostpointercapture={cancel} onkeydown={e => keyboard(e,edge as GanttGesture)} onclick={e => e.stopPropagation()}></button>
  {/each}
  {#if drag}<span class="dates" role="status">{dateFromDay(range.from)} – {dateFromDay(range.to-1)}</span>{/if}
</div>
<style>
 .task-frame { position:absolute; height:30px; line-height:30px; }
 .task { display:block; position:absolute; inset:0; width:100%; height:30px; box-sizing:border-box; border:1px solid #709981; border-radius:4px; background:#dfebdf; color:#203e33; padding:3px 10px; font:inherit; line-height:22px; text-align:left; overflow:hidden; text-overflow:ellipsis; cursor:grab; touch-action:none; user-select:none; }
 .dragging { z-index:3; } .dragging .task { cursor:grabbing; box-shadow:0 2px 8px #203e3340; }
 .edge { position:absolute; top:0; bottom:0; width:min(8px,25%); border:0; border-radius:3px; background:transparent; padding:0; cursor:ew-resize; touch-action:none; }
 .start { left:0; } .end { right:0; }
 .edge:hover, .edge:focus-visible { background:#235f4960; }
 button:focus-visible { outline:2px solid #235f49; outline-offset:-2px; }
 .dates { position:absolute; bottom:34px; left:0; background:#203e33; color:white; border-radius:3px; padding:2px 6px; font-size:12px; line-height:20px; pointer-events:none; }
</style>
