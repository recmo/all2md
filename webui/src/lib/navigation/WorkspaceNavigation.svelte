<script lang="ts">
  import { onMount } from 'svelte';
  import { FileTree } from '@pierre/trees';
  let {
    selected,
    onopen,
    onimport,
    disabled = false
  }: {
    selected: 'document' | 'search' | 'settings' | 'submit';
    disabled?: boolean;
    onimport: () => void;
    onopen: (view: 'search' | 'settings' | 'submit') => void;
  } = $props();
  let host: HTMLDivElement;
  let model = $state.raw<FileTree>();
  let syncing = false;
  onMount(() => {
    const tree = new FileTree({
      paths: ['Search', 'Settings', 'Submit', 'Import'],
      icons: {
        set: 'none',
        spriteSheet:
          '<svg data-icon-sprite aria-hidden="true" width="0" height="0" xmlns="http://www.w3.org/2000/svg"><symbol id="workspace-search" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="10" cy="10" r="6.5"/><path d="m15 15 6 6"/></g></symbol><symbol id="workspace-settings" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 6h18M3 12h18M3 18h18"/><path d="M8 3v6m8 0v6m-8 0v6"/></g></symbol><symbol id="workspace-submit" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/></g></symbol><symbol id="workspace-import" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/></g></symbol></svg>',
        byFileName: {
          Import: { name: 'workspace-import', viewBox: '0 0 24 24' },
          Submit: { name: 'workspace-submit', viewBox: '0 0 24 24' },
          Search: { name: 'workspace-search', viewBox: '0 0 24 24' },
          Settings: { name: 'workspace-settings', viewBox: '0 0 24 24' }
        }
      },
      renaming: false,
      dragAndDrop: false,
      onSelectionChange: (paths) => {
        if (syncing || disabled) return;
        const path = paths.at(-1);
        if (path === 'Import') {
          syncing = true;
          tree.getItem(path)?.deselect();
          syncing = false;
          onimport();
        } else if (path)
          onopen(path.toLowerCase() as 'search' | 'settings' | 'submit');
      }
    });
    tree.render({ containerWrapper: host });
    const container = tree.getFileTreeContainer();
    if (container) {
      container.setAttribute('aria-label', 'Workspace');
      container.style.height = '100%';
      container.style.setProperty('--trees-padding-inline', '8px');
      container.style.setProperty('--trees-item-margin-x', '0px');
      container.style.setProperty('--trees-item-padding-x', '6px');
      container.style.setProperty('--trees-bg-override', '#f5f3ec');
      container.style.setProperty('--trees-accent-override', '#235f49');
    }
    model = tree;
    return () => tree.cleanUp();
  });
  $effect(() => {
    const tree = model;
    if (!tree) return;
    syncing = true;
    const path =
      selected === 'document'
        ? null
        : selected[0].toUpperCase() + selected.slice(1);
    for (const current of tree.getSelectedPaths())
      if (current !== path) tree.getItem(current)?.deselect();
    if (path) tree.getItem(path)?.select();
    syncing = false;
  });
</script>

<div class="workspace-navigation" bind:this={host} inert={disabled}></div>

<style>
  .workspace-navigation {
    height: 124px;
    flex: 0 0 124px;
  }
</style>
