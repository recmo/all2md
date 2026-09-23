<script lang="ts">
  import AppTable from './AppTable.svelte';
  import AppKanban from './AppKanban.svelte';
  import AppGantt from './AppGantt.svelte';
  import type { Cache } from './cache';
  import { appDocuments, evaluateApp, field, display, type AppResult, type AppDocument, type AppEdit } from './apps';
  let { path, cache, online, busy, onopen, onstage }: { path: string; cache: Cache; online: boolean; busy: boolean; onopen: (path: string) => void; onstage: (edits: AppEdit[]) => Promise<void> } = $props();
  let tab = $state(''), result = $state<AppResult>(), error = $state(''), loading = $state(false), acting = $state(false), notice = $state('');
  const inventory = $derived(appDocuments(cache));
  const source = $derived(path ? (cache.drafts[path] || cache.pages[path])?.text : undefined);
  $effect(() => {
    const documents = inventory.documents, text = source, filename = path;
    let stale = false;
    loading = true; error = '';
    const timer = setTimeout(async () => {
      if (!filename || !text) { result = undefined; loading = false; return; }
      try {
        const value = await evaluateApp(filename, text, documents);
        if (!stale) { result = value; error = ''; }
      } catch (e) { if (!stale) { error = String(e); result = undefined; } }
      finally { if (!stale) loading = false; }
    }, 200);
    return () => { stale = true; clearTimeout(timer); };
  });
  const view = $derived(result?.views.find(v => v.name === tab) || result?.views[0]);
  const rows = $derived((view ? result?.collections[view.collection] || [] : []).map(path => inventory.documents.find(d => d.path === path)).filter((d): d is AppDocument => !!d));
  const columns = $derived(view?.bindings.columns || [...new Set(rows.map(d => display(field(d, view?.bindings.group))))]);
  const groups = $derived([...new Set([...columns, ...rows.map(d => display(field(d, view?.bindings.group)))])]);
  async function reschedule(doc: AppDocument, from: string, through: string) {
    if (!view || loading || acting || busy) return;
    const {start, end} = view.bindings;
    if (!start?.startsWith('/') || !end?.startsWith('/') || start === end) return;
    acting = true; error = ''; notice = '';
    try {
      await onstage([{path: doc.path, fields: {}, pointers: {[start]: from, [end]: through},
        expected: {[start]: field(doc, start), [end]: field(doc, end)}}]);
      notice = 'Changes staged. Review validation and submit when ready.';
    } catch (e) { error = String(e); } finally { acting = false; }
  }
  async function move(doc: AppDocument, value: string) {
    if (!view?.bindings.on_move || !path || !source || loading || acting || busy) return;
    const originalDrafts = cache.drafts, originalDeletions = cache.deletions;
    acting = true; error = ''; notice = '';
    try {
      const output = await evaluateApp(path, source, inventory.documents, view.bindings.on_move, { path: doc.path, value, timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z') });
      if (!Array.isArray(output.edits)) throw Error('Action must return a list of proposed edits');
      if (cache.drafts !== originalDrafts || cache.deletions !== originalDeletions) throw Error('Drafts changed while evaluating the action. Try again.');
      await onstage(output.edits);
      notice = 'Changes staged. Review validation and submit when ready.';
    } catch (e) { error = String(e); } finally { acting = false; }
  }
</script>
<div class="workspace-page apps-page">
    <p class="muted">{inventory.documents.length} documents · {online ? 'Includes local drafts' : 'Offline · cached inventory; server changes may be missing'}</p>
    {#if inventory.missing.length}<p role="alert">Incomplete collection: metadata unavailable for {inventory.missing.join(', ')}.</p>{/if}
    {#each inventory.errors as issue}<p class="error" role="alert">{issue}</p>{/each}
    {#if source === undefined}<p role="alert">App definition is not cached. Reconnect to load it.</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    {#if notice}<p role="status">{notice}</p>{/if}
    <nav class="app-tabs" aria-label="App views">
      {#each result?.views || [] as option}<button aria-pressed={option.name === view?.name} onclick={() => tab = option.name}>{option.name}</button>{/each}
    </nav>
    {#if loading}<p role="status">Updating views…</p>{/if}
    {#if view}
      <h2>{view.name}</h2>
      {#if !rows.length}<p>No documents in this collection.</p>{/if}
      {#if view.kind === 'table'}
        <AppTable {rows} bindings={columns} {onopen} />
      {:else if view.kind === 'kanban'}
        <AppKanban {rows} group={view.bindings.group!} columns={groups} disabled={acting || loading || busy} {onopen} onmove={view.bindings.on_move ? (doc,value) => void move(doc,value) : undefined} />
      {:else if view.kind === 'gantt'}
        {#key view.name}
          <AppGantt {rows} disabled={acting || loading || busy} onchange={reschedule} group={view.bindings.group} dependencies={view.bindings.dependencies} start={view.bindings.start!} end={view.bindings.end!} {onopen} />
        {/key}
      {/if}
    {/if}
</div>
