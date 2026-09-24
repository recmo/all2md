<script lang="ts">
  import { Grid, Willow } from '@svar-ui/svelte-grid';
  import AppDocCell from './AppDocCell.svelte';
  import { field, display, type AppDocument } from './apps';
  let { rows, bindings, onopen }: { rows: AppDocument[]; bindings: string[]; onopen: (path: string) => void } = $props();
  const columns = $derived([{ id: 'title', header: 'Document', width: 260, cell: AppDocCell, sort: true }, ...bindings.filter(b => b !== 'title').map((binding, i) => ({ id: 'c'+i, header: binding.replace(/^\//,''), width: 180, sort: true }))]);
  const data = $derived(rows.map(doc => ({ id: doc.path, title: doc.title, open: () => onopen(doc.path), ...Object.fromEntries(bindings.filter(b => b !== 'title').map((binding,i) => ['c'+i, display(field(doc,binding))])) })));
</script>
<div class="svar-view" aria-label="Collection table"><Willow><Grid {data} {columns} /></Willow></div>
