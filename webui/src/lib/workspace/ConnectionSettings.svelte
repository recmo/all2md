<script lang="ts">
  import { api } from '$lib/workspace/api';
  let { onconnect }: { onconnect: () => Promise<void> } = $props();
  let token = $state('');
  let busy = $state(false);
  let message = $state('');
  let failed = $state(false);
  async function connect(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    failed = false;
    const credential = token;
    token = '';
    try {
      await api.login(credential);
      await onconnect();
      message = 'Connected. This browser session lasts up to 12 hours.';
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
  <p class="muted">
    Your API key is exchanged for an HttpOnly session cookie and is not stored
    in the browser.
  </p>
  <button
    disabled={busy}
    onclick={async () => {
      busy = true;
      try {
        await api.logout();
        message = 'Signed out.';
        failed = false;
      } catch (e) {
        message = String(e);
        failed = true;
      } finally {
        busy = false;
      }
    }}>Sign out</button
  >
  {#if message}<p class:error={failed} role="status">{message}</p>{/if}
</section>
