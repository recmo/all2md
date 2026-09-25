<script lang="ts">
  import type { ICellProps } from '@svar-ui/svelte-grid';
  let { row }: ICellProps = $props();
</script>

<button
  onclick={(event) => {
    event.stopPropagation();
    row.open();
  }}>{row.title}</button
>
