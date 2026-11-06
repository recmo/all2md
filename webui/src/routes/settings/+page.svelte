<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  onMount(() => {
    void goto(base + '/#@settings', { replaceState: true });
  });
</script>
