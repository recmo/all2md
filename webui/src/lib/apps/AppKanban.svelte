<script lang="ts">
  import {
    Kanban,
    Willow,
    type KanbanInstanceApi
  } from '@svar-ui/svelte-kanban';
  import AppCard from './AppCard.svelte';
  import { field, display, type AppDocument } from './apps';
  let {
    rows,
    group,
    columns,
    disabled,
    onopen,
    onmove
  }: {
    rows: AppDocument[];
    group: string;
    columns: string[];
    disabled: boolean;
    onopen: (path: string) => void;
    onmove?: (doc: AppDocument, value: string) => void;
  } = $props();
  const cards = $derived(
    rows.map((doc) => ({
      id: doc.path,
      label: doc.title,
      column: display(field(doc, group)),
      open: () => onopen(doc.path),
      move: onmove ? (value: string) => onmove?.(doc, value) : undefined,
      targets: columns,
      disabled
    }))
  );
  const stages = $derived(
    columns.map((id) => ({ id, label: id, addCard: false }))
  );
  function init(api: KanbanInstanceApi) {
    api.intercept('move-card', (event) => {
      const move = event as { id: string; column?: string };
      const doc = rows.find((d) => d.path === move.id);
      if (
        !disabled &&
        doc &&
        move.column !== undefined &&
        move.column !== display(field(doc, group))
      )
        onmove?.(doc, move.column);
      // Only workspace changes may update the board: no independent component store writes.
      return false;
    });
    for (const action of [
      'add-card',
      'delete-card',
      'update-card',
      'update-column'
    ] as const)
      api.intercept(action, () => false);
  }
</script>

<div class="svar-view" aria-label="Collection board">
  <Willow
    ><Kanban
      {cards}
      columns={stages}
      cardContent={AppCard}
      {init}
      readonly={disabled || !onmove}
    /></Willow
  >
</div>
