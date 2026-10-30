<script lang="ts">
  import '../app.css';
  let { children } = $props();
</script>

<svelte:head><link rel="stylesheet" href="/fonts/fonts.css" /></svelte:head>

{@render children()}
