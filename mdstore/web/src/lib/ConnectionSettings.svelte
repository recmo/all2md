<script lang="ts">
  import { api } from '$lib/api';
  let { onconnect }: { onconnect: () => Promise<void> } = $props();
  let token = $state('');
  let busy = $state(false);
  let message = $state('');
  let failed = $state(false);
  async function connect(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    failed = false;
    const previous = api.token;
    api.token = token;
    token = '';
    try {
      await onconnect();
      message =
        'Connected. Your token is available until this tab is reloaded or closed.';
    } catch (error) {
      api.token = previous;
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
  <form class="connection-form" onsubmit={connect}>
    <label for="token">Bearer token</label>
    <input
      id="token"
      type="password"
      bind:value={token}
      autocomplete="off"
      placeholder="Optional for local daemons"
      disabled={busy}
    />
    <button class="primary" disabled={busy}
      >{busy ? 'Connecting…' : 'Connect'}</button
    >
  </form>
  <p class="muted">Kept in memory only. Never saved in browser storage.</p>
  {#if message}<p class:error={failed} role="status">{message}</p>{/if}
</section>
