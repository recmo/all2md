<script lang="ts">
  let { onconnect }: { onconnect: () => Promise<void> } = $props();
  let busy = $state(false);
  let message = $state('');
  let failed = $state(false);
  async function connect() {
    busy = true;
    failed = false;
    try {
      await onconnect();
      message = 'Connected.';
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
      failed = true;
    } finally {
      busy = false;
    }
  }
</script>

<section class="review">
  <h2>Connection</h2>
  <p>Connect to the mdstore daemon serving this workspace.</p>
  <button class="primary" disabled={busy} onclick={connect}
    >{busy ? 'Connecting…' : 'Connect'}</button
  >
  {#if message}<p class:error={failed} role="status">{message}</p>{/if}
</section>
