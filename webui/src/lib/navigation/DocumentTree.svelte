<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { treeStatuses } from './treeStatus';
  import { FileTree, type FileTreeCompositionOptions } from '@pierre/trees';

  let {
    paths,
    deleted = [],
    conflicts = [],
    validation,
    selected,
    drafts,
    cached,
    fetching,
    saved,
    busy,
    oncreate,
    onaction,
    onmove,
    ondelete,
    onopen
  }: {
    paths: string[];
    deleted?: string[];
    conflicts?: string[];
    validation?: { valid: boolean; invalid: string[]; pending: boolean };
    selected?: string;
    drafts: string[];
    cached: string[];
    fetching: string[];
    saved: boolean;
    oncreate: (folder: string) => void;
    onaction: (
      action: 'folder' | 'move',
      path: string,
      directory: boolean
    ) => void;
    onmove: (
      moves: { from: string; to: string; directory: boolean }[]
    ) => Promise<void>;
    ondelete: (path: string, directory: boolean) => Promise<void>;
    busy: boolean;
    onopen: (path: string) => void;
  } = $props();
  let host: HTMLDivElement;
  let model = $state.raw<FileTree>();
  let syncing = false;
  let dragging = false;
  let staging = false;
  let moveError = $state('');
  let previousPaths = '';
  const statuses = $derived(
    treeStatuses(
      paths.filter((path) => !path.endsWith('/')),
      cached,
      drafts,
      fetching,
      saved,
      deleted,
      validation,
      conflicts
    )
  );
  const deletedItem = (path: string) =>
    deleted.includes(path.replace(/\/+$/, ''));
  const containsDeleted = (path: string) =>
    deletedItem(path) ||
    deleted.some((item) => item.startsWith(path.replace(/\/+$/, '') + '/'));
  let disposeMenu = () => {};
  const composition: FileTreeCompositionOptions = {
    contextMenu: {
      enabled: true,
      triggerMode: 'right-click',
      onClose: () => disposeMenu(),
      render(item, context) {
        const menu = document.createElement('div');
        menu.className = 'tree-context-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('popover', 'manual');
        menu.dataset.fileTreeContextMenuRoot = 'true';
        // The tree's anchor is only a trigger-sized slot. Use the browser's
        // top layer so the menu has its own size and escapes sidebar clipping.
        const frame = requestAnimationFrame(() => {
          if (!menu.isConnected) return;
          menu.showPopover();
          const rect = menu.getBoundingClientRect();
          menu.style.left =
            Math.max(
              8,
              Math.min(
                context.anchorRect.left,
                window.innerWidth - rect.width - 8
              )
            ) + 'px';
          menu.style.top =
            Math.max(
              8,
              Math.min(
                context.anchorRect.bottom + 4,
                window.innerHeight - rect.height - 8
              )
            ) + 'px';
          button.focus();
        });
        const dismiss = () => context.close();
        window.addEventListener('resize', dismiss);
        disposeMenu = () => {
          cancelAnimationFrame(frame);
          window.removeEventListener('resize', dismiss);
          if (menu.matches(':popover-open')) menu.hidePopover();
        };
        menu.addEventListener('keydown', (event) => {
          if (event.key === 'Escape') {
            event.preventDefault();
            context.close();
          }
        });
        const button = document.createElement('button');
        button.textContent = deletedItem(item.path)
          ? 'Review deletion'
          : 'New document here…';
        button.setAttribute('role', 'menuitem');
        button.disabled = busy;
        button.onclick = () => {
          if (deletedItem(item.path)) {
            context.close();
            onopen(item.path);
            return;
          }
          const folder =
            item.kind === 'directory'
              ? item.path
              : item.path.split('/').slice(0, -1).join('/');
          context.close({ restoreFocus: false });
          oncreate(folder.replace(/\/+$/, ''));
        };
        menu.append(button);
        for (const [label, action] of [
          ['New folder here…', 'folder'],
          ['Rename…', 'rename'],
          ['Move…', 'move'],
          ['Delete', 'delete']
        ] as const) {
          if (deletedItem(item.path) || (!item.path && action !== 'folder'))
            continue;
          const entry = document.createElement('button');
          entry.textContent = label;
          entry.setAttribute('role', 'menuitem');
          entry.disabled = busy;
          entry.onclick = () => {
            context.close({ restoreFocus: false });
            if (action === 'rename') {
              moveError = '';
              model?.startRenaming(item.path);
              return;
            }
            const path = item.path.replace(/\/+$/, '');
            if (action === 'delete') {
              moveError = '';
              void ondelete(path, item.kind === 'directory').catch((error) => {
                moveError =
                  error instanceof Error ? error.message : String(error);
              });
              return;
            }
            onaction(
              action,
              action === 'folder' && item.kind !== 'directory'
                ? path.split('/').slice(0, -1).join('/')
                : path,
              item.kind === 'directory'
            );
          };
          menu.append(entry);
        }
        return menu;
      }
    }
  };

  onMount(() => {
    function stage(moves: { from: string; to: string; directory: boolean }[]) {
      staging = true;
      moveError = '';
      // Trees mutates its model optimistically. Restore the authoritative
      // paths while the client fetches sources and stages the whole batch.
      queueMicrotask(async () => {
        tree.resetPaths(paths);
        try {
          await onmove(moves);
        } catch (error) {
          moveError = error instanceof Error ? error.message : String(error);
        } finally {
          tree.resetPaths(paths);
          previousPaths = JSON.stringify(paths);
          if (selected) tree.getItem(selected)?.select();
          staging = false;
          dragging = false;
        }
      });
    }
    const tree = new FileTree({
      paths,
      initialExpansion: 'open',
      initialSelectedPaths: selected ? [selected] : [],
      density: 'default',
      unsafeCSS: `[data-item-section="content"] { flex: 1 1 0; min-width: 0; }
        [data-item-section="decoration"] { flex: 0 0 auto; min-width: max-content; margin-left: 8px; font-weight: 700; overflow: visible; }
        [data-item-section="decoration"] > span { overflow: visible; white-space: pre; }`,
      composition,
      renderRowDecoration: ({ item }) =>
        statuses.get(item.path.replace(/\/+$/, '')) || null,
      renaming: {
        canRename: (item) => !busy && !staging && !containsDeleted(item.path),
        onError: (error) => {
          moveError = error;
        },
        onRename: (event) =>
          stage([
            {
              from: event.sourcePath.replace(/\/+$/, ''),
              to: event.destinationPath.replace(/\/+$/, ''),
              directory: event.isFolder
            }
          ])
      },
      dragAndDrop: {
        canDrag: (items) => !busy && !staging && !items.some(containsDeleted),
        canDrop: () => !busy && !staging,
        onDropError: (error) => {
          moveError = error;
        },
        onDropComplete: (event) => {
          const folder = (event.target.directoryPath || '').replace(/\/+$/, '');
          const moves = event.draggedPaths.map((path) => {
            const from = path.replace(/\/+$/, '');
            return {
              from,
              to: [folder, from.split('/').at(-1)!].filter(Boolean).join('/'),
              directory: path.endsWith('/')
            };
          });
          stage(moves);
        }
      },
      onSelectionChange: (selection) => {
        if (syncing || busy || dragging || staging) return;
        const path = selection.at(-1);
        if (path && !tree.getItem(path)?.isDirectory() && path !== selected)
          onopen(path);
      }
    });
    previousPaths = JSON.stringify(paths);
    tree.render({ containerWrapper: host });
    const container = tree.getFileTreeContainer();
    if (container) {
      container.style.height = '100%';
      container.style.width = '100%';
      container.style.setProperty('--trees-padding-inline', '8px');
      container.style.setProperty('--trees-item-margin-x', '0px');
      container.style.setProperty('--trees-item-padding-x', '6px');
      container.style.setProperty('--trees-bg-override', '#f5f3ec');
      container.style.setProperty('--trees-accent-override', '#235f49');
    }
    const doubleClick = (event: MouseEvent) => {
      if (
        busy ||
        staging ||
        event.composedPath().some((node) => node instanceof HTMLInputElement)
      )
        return;
      const row = event
        .composedPath()
        .find(
          (node) =>
            node instanceof HTMLElement && node.hasAttribute('data-item-path')
        ) as HTMLElement | undefined;
      const path = row?.dataset.itemPath;
      if (!path || containsDeleted(path)) return;
      event.preventDefault();
      event.stopPropagation();
      moveError = '';
      tree.startRenaming(path);
    };
    const dragStart = () => {
      dragging = true;
      moveError = '';
    };
    const dragEnd = () => {
      if (!staging) dragging = false;
    };
    let rootMenu: HTMLElement | null = null;
    const closeRootMenu = () => {
      if (!rootMenu) return;
      disposeMenu();
      rootMenu.remove();
      rootMenu = null;
    };
    const outsideRootMenu = (event: PointerEvent) => {
      if (rootMenu && !event.composedPath().includes(rootMenu)) closeRootMenu();
    };
    const rootContextMenu = (event: MouseEvent) => {
      if (
        busy ||
        event
          .composedPath()
          .some(
            (node) =>
              node instanceof HTMLElement && node.hasAttribute('data-item-path')
          )
      )
        return;
      event.preventDefault();
      closeRootMenu();
      rootMenu = composition.contextMenu!.render!(
        { kind: 'directory', name: '', path: '' },
        {
          anchorElement: host,
          anchorRect: {
            x: event.clientX,
            y: event.clientY,
            left: event.clientX,
            right: event.clientX,
            top: event.clientY,
            bottom: event.clientY,
            width: 0,
            height: 0
          },
          close: closeRootMenu,
          restoreFocus: () => host.focus()
        }
      );
      if (rootMenu) host.append(rootMenu);
    };
    host.addEventListener('contextmenu', rootContextMenu);
    window.addEventListener('pointerdown', outsideRootMenu, true);
    host.addEventListener('dblclick', doubleClick, true);
    host.addEventListener('dragstart', dragStart, true);
    host.addEventListener('dragend', dragEnd, true);
    model = tree;
    return () => {
      closeRootMenu();
      disposeMenu();
      host.removeEventListener('contextmenu', rootContextMenu);
      window.removeEventListener('pointerdown', outsideRootMenu, true);
      host.removeEventListener('dblclick', doubleClick, true);
      host.removeEventListener('dragstart', dragStart, true);
      host.removeEventListener('dragend', dragEnd, true);
      tree.cleanUp();
    };
  });

  $effect(() => {
    const tree = model;
    const next = JSON.stringify(paths);
    const current = selected;
    if (!tree) return;
    syncing = true;
    try {
      if (next !== previousPaths) {
        tree.resetPaths(paths);
        previousPaths = next;
      }
      untrack(() => {
        for (const path of tree.getSelectedPaths()) {
          if (path !== current) tree.getItem(path)?.deselect();
        }
        if (current) {
          const parts = current.split('/');
          for (let i = 1; i < parts.length; i++) {
            const folder = tree.getItem(parts.slice(0, i).join('/'));
            if (folder && 'expand' in folder) folder.expand();
          }
          tree.getItem(current)?.select();
        }
      });
    } finally {
      syncing = false;
    }
  });

  $effect(() => {
    const container = model?.getFileTreeContainer();
    const root = container?.shadowRoot;
    if (!root) return;
    let style = root.querySelector<HTMLStyleElement>(
      'style[data-staged-deletions]'
    );
    if (!style) {
      style = document.createElement('style');
      style.dataset.stagedDeletions = '';
      root.append(style);
    }
    const removed = new Set(deleted);
    for (const path of deleted) {
      const parts = path.split('/');
      for (let i = 1; i < parts.length; i++) {
        const folder = parts.slice(0, i).join('/') + '/';
        if (
          paths
            .filter((item) => item.startsWith(folder) && !item.endsWith('/'))
            .every((item) => deleted.includes(item))
        )
          removed.add(folder);
      }
    }
    style.textContent = [...removed]
      .map(
        (path) =>
          `[data-item-path="${CSS.escape(path)}"] [data-item-section="content"] { text-decoration: line-through; opacity: 0.65; }`
      )
      .join('\n');
  });

  $effect(() => {
    // Decorations read the current cache state; re-render through the public
    // composition API without rebuilding the model or resetting expansion.
    statuses;
    model?.setComposition({ ...composition });
  });
</script>

<div class="document-tree-host" bind:this={host} inert={busy}></div>
{#if moveError}<p role="alert">{moveError}</p>{/if}

<style>
  .document-tree-host {
    height: 100%;
    min-height: 160px;
  }
</style>
