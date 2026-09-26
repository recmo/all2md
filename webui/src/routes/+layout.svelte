<script lang="ts">
  import '../app.css';
  import { base } from '$app/paths';
  import { onMount } from 'svelte';
  onMount(() => {
    // Retire the former root-scoped shell worker, which would intercept GET /.
    if (
      navigator.serviceWorker?.controller &&
      new URL(navigator.serviceWorker.controller.scriptURL).pathname ===
        '/service-worker.js'
    ) {
      void navigator.serviceWorker
        .getRegistrations()
        .then(async (registrations) => {
          for (const registration of registrations) {
            if (
              new URL(registration.scope).pathname === '/' &&
              registration.active &&
              new URL(registration.active.scriptURL).pathname ===
                '/service-worker.js'
            )
              await registration.unregister();
          }
          location.reload();
        });
    }
  });
  let { children } = $props();
</script>

<svelte:head
  ><link rel="stylesheet" href={base + '/fonts/fonts.css'} /></svelte:head
>

{@render children()}
