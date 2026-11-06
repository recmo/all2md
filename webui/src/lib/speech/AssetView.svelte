<script lang="ts">
  import { fileUrl, type Api, type Page } from '../workspace/api';
  let { page }: { page: Page; api: Api } = $props();
  const url = $derived(fileUrl(page.path));
</script>

<section>
  <p>{page.path} · {((page.asset?.size || 0) / 1024 / 1024).toFixed(2)} MiB</p>
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
