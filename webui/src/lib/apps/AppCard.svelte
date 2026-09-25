<script lang="ts">
  import type { KanbanCard } from '@svar-ui/svelte-kanban';
  let { card }: { card: KanbanCard } = $props();
</script>

<div class="app-card-content">
  <button onclick={() => card.open()}>{card.label}</button>
  {#if card.move}<label
      >Move to<select
        aria-label={`Move ${card.label}`}
        value={card.column}
        disabled={card.disabled}
        onchange={(event) => card.move(event.currentTarget.value)}
      >
        {#each card.targets as target}<option value={target}>{target}</option
          >{/each}
      </select></label
    >{/if}
</div>

<style>
  .app-card-content {
    padding: 12px;
  }
  button {
    border: 0;
    padding: 0;
    text-align: left;
    font-weight: 600;
    margin-bottom: 12px;
  }
  select {
    display: block;
    width: 100%;
  }
</style>
