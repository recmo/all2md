<script lang="ts">
  import '../app.css';
  import { base } from '$app/paths';
  let { children } = $props();
</script>

<svelte:head
  ><link rel="stylesheet" href={base + '/fonts/fonts.css'} /></svelte:head
>

{@render children()}
