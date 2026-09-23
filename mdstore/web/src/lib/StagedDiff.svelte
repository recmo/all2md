<script lang="ts">
  import './pierre';
  import { onMount } from 'svelte';
  import { FileDiff } from '@pierre/diffs';
  let {
    path,
    before,
    after
  }: { path: string; before: string | null; after: string | null } = $props();
  let host: HTMLDivElement;
  let viewer = $state.raw<FileDiff>();
  onMount(() => {
    const diff = new FileDiff({
      theme: 'pierre-light',
      diffStyle: 'unified',
      overflow: 'wrap',
      disableFileHeader: true
    });
    viewer = diff;
    return () => diff.cleanUp();
  });
  $effect(() => {
    if (!viewer) return;
    const oldFile = before === null ? null : { name: path, contents: before };
    const newFile = after === null ? null : { name: path, contents: after };
    if (oldFile && newFile)
      viewer.render({ oldFile, newFile, containerWrapper: host });
    else if (oldFile)
      viewer.render({ oldFile, newFile: null, containerWrapper: host });
    else if (newFile)
      viewer.render({ oldFile: null, newFile, containerWrapper: host });
  });
</script>

<div class="staged-diff" bind:this={host}></div>
