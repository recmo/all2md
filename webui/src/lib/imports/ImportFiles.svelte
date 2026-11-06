<script lang="ts">
  import { onMount } from 'svelte';
  import {
    droppedFiles,
    pickedFiles,
    importPath,
    importKind,
    type ImportFile
  } from './files';
  let {
    busy,
    paths,
    onshow,
    onimport
  }: {
    busy: boolean;
    onshow: () => void;
    paths: string[];
    onimport: (
      files: ImportFile[],
      report: (path: string, status: string) => void
    ) => Promise<void>;
  } = $props();
  let dialog: HTMLDialogElement;
  let files = $state.raw<ImportFile[]>([]);
  let destination = $state('');
  let reading = $state(false),
    importing = $state(false),
    hovering = $state(false);
  let error = $state('');
  let statuses = $state<Record<string, string>>({});
  const planned = $derived(
    files.map((item) => ({
      ...item,
      path: destination
        ? destination.replace(/\/+$/, '') + '/' + item.path
        : item.path
    }))
  );
  export function open() {
    onshow();
    files = [];
    statuses = {};
    error = '';
    destination = '';
    dialog.showModal();
  }
  async function collect(pending: Promise<ImportFile[]>) {
    reading = true;
    files = [];
    error = '';
    statuses = {};
    try {
      files = await pending;
    } catch (e) {
      error = String(e);
    } finally {
      reading = false;
    }
  }
  async function submit() {
    error = '';
    try {
      const entries = files
        .filter((item) => importKind(item.path))
        .map((item) => ({
          ...item,
          path: importPath(destination, item.path)
        }))
        .filter(
          (item) => !['Staged', 'Uploaded'].includes(statuses[item.path])
        );
      const unique = new Set(entries.map((item) => item.path));
      if (unique.size !== entries.length)
        throw Error('Multiple files have the same destination.');
      importing = true;
      await onimport(
        entries.filter((item) => importKind(item.path)),
        (path, status) => (statuses = { ...statuses, [path]: status })
      );
    } catch (e) {
      error = String(e);
    } finally {
      importing = false;
    }
  }
  onMount(() => {
    const external = (e: DragEvent) => e.dataTransfer?.types.includes('Files');
    const over = (e: DragEvent) => {
      if (!external(e)) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.dataTransfer)
        e.dataTransfer.dropEffect =
          busy || reading || importing ? 'none' : 'copy';
      hovering = !busy && !reading && !importing;
    };
    const leave = (e: DragEvent) => {
      if (!e.relatedTarget) hovering = false;
    };
    const drop = (e: DragEvent) => {
      if (!external(e)) return;
      e.preventDefault();
      e.stopPropagation();
      hovering = false;
      if (busy || reading || importing) return;
      const row = e
        .composedPath()
        .find(
          (node) =>
            node instanceof HTMLElement && node.hasAttribute('data-item-path')
        ) as HTMLElement | undefined;
      const path = row?.dataset.itemPath || '';
      open();
      destination =
        paths.some((item) => item.startsWith(path + '/')) || path.endsWith('/')
          ? path.replace(/\/+$/, '')
          : path.split('/').slice(0, -1).join('/');
      void collect(droppedFiles(e.dataTransfer!));
    };
    window.addEventListener('dragover', over, true);
    window.addEventListener('dragleave', leave, true);
    window.addEventListener('drop', drop, true);
    return () => {
      window.removeEventListener('dragover', over, true);
      window.removeEventListener('dragleave', leave, true);
      window.removeEventListener('drop', drop, true);
    };
  });
</script>

{#if hovering}<div class="drop-hint">Drop files or folders to import</div>{/if}
<dialog
  bind:this={dialog}
  oncancel={(event) => {
    if (importing || reading) event.preventDefault();
  }}
  aria-label="Import files"
>
  <h2>Import files</h2>
  <p>
    Folders keep their hierarchy. Markdown is staged for validation and
    submission; media is uploaded immediately. Existing files are never
    replaced. Empty folders are not imported.
  </p>
  <fieldset disabled={importing || reading || Object.keys(statuses).length > 0}>
    <label
      >Destination folder<input
        placeholder="Repository root"
        bind:value={destination}
      /></label
    >
    <label
      >Choose files<input
        type="file"
        multiple
        onchange={(event) =>
          void collect(
            Promise.resolve(pickedFiles(event.currentTarget.files || []))
          )}
      /></label
    >
    <label
      >Choose folder<input
        type="file"
        webkitdirectory
        onchange={(event) =>
          void collect(
            Promise.resolve(pickedFiles(event.currentTarget.files || []))
          )}
      /></label
    >
  </fieldset>
  {#if reading}<p role="status">Reading folder…</p>{/if}
  <ul>
    {#each planned as item}<li>
        <span>{item.path}</span><small
          >{statuses[item.path] ||
            (importKind(item.path) === 'markdown'
              ? 'Stage Markdown'
              : importKind(item.path)
                ? 'Upload asset'
                : 'Skipped: unsupported file')}</small
        >
      </li>{/each}
  </ul>
  {#if error}<p role="alert">{error}</p>{/if}
  <div class="actions">
    <button
      disabled={busy ||
        reading ||
        importing ||
        !planned.some(
          (item) =>
            importKind(item.path) &&
            !['Staged', 'Uploaded'].includes(statuses[item.path])
        )}
      onclick={submit}>Import</button
    ><button disabled={importing || reading} onclick={() => dialog.close()}
      >Close</button
    >
  </div>
</dialog>

<style>
  dialog {
    width: min(38rem, calc(100vw - 2rem));
    max-height: 85dvh;
    overflow: auto;
    border: 1px solid #cbd4c8;
    border-radius: 12px;
    padding: 1.5rem;
    color: inherit;
    background: #fafbf7;
  }
  dialog::backdrop {
    background: #0005;
  }
  h2 {
    margin-top: 0;
  }
  p,
  small {
    font-size: 0.85rem;
  }
  fieldset {
    border: 0;
    padding: 0;
    display: grid;
    gap: 1rem;
  }
  label {
    display: grid;
    gap: 0.35rem;
  }
  input {
    max-width: 100%;
    min-width: 0;
  }
  ul {
    padding: 0;
    list-style: none;
    max-height: 30vh;
    overflow: auto;
  }
  li {
    display: grid;
    padding: 0.35rem 0;
    overflow-wrap: anywhere;
  }
  small {
    opacity: 0.75;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
  }
  .drop-hint {
    position: fixed;
    inset: 1rem;
    z-index: 10000;
    pointer-events: none;
    border: 3px dashed #246b50;
    border-radius: 12px;
    background: #e4efe4dd;
    display: grid;
    place-items: center;
    font-weight: bold;
  }
</style>
