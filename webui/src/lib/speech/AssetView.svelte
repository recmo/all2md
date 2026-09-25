<script lang="ts">
  import { onMount } from 'svelte';
  import type { Api, Page } from '../workspace/api';
  let { page, api }: { page: Page; api: Api } = $props();
  let url = $state(''),
    error = $state('');
  onMount(() => {
    let active = true;
    void api
      .request<{ url: string }>(
        '/assets/ticket?path=' + encodeURIComponent(page.path),
        { method: 'POST' }
      )
      .then((ticket) => {
        if (active) url = ticket.url;
      })
      .catch((e) => {
        if (active) error = String(e);
      });
    return () => {
      active = false;
    };
  });
</script>

<section>
  <p>{page.path} · {((page.asset?.size || 0) / 1024 / 1024).toFixed(2)} MiB</p>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if url}
    {#if /\.(mp4|webm)$/i.test(page.path)}
      <!-- Original uploads may precede transcription; no caption track exists yet. -->
      <!-- svelte-ignore a11y_media_has_caption -->
      <video controls src={url} aria-label="Recording playback"></video>
    {:else if /\.(m4a|mp3|wav|flac|ogg|aac)$/i.test(page.path)}<audio
        controls
        src={url}
        aria-label="Recording playback"
      ></audio>{/if}
    <p>
      <a href={url} download={page.path.split('/').at(-1)}>Download original</a>
    </p>
  {/if}
</section>

<style>
  section {
    padding: 1.5rem;
  }
  video,
  audio {
    width: 100%;
  }
  video {
    max-height: 70vh;
  }
</style>
