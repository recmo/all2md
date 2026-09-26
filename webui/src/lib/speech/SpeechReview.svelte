<script lang="ts">
  import { onMount } from 'svelte';
  import { subscribeChanges } from '../workspace/events';
  import { fileUrl, type Api, type Page } from '../workspace/api';
  import type { AppEdit } from '../apps/apps';
  import {
    properties,
    sibling,
    turns,
    type Turn,
    type Attendee
  } from './recording';
  let {
    page,
    api,
    dirty,
    onstage,
    onopen
  }: {
    page: Page;
    api: Api;
    dirty: boolean;
    onstage: (edits: AppEdit[]) => Promise<void>;
    onopen: (path: string) => void;
  } = $props();
  type Job = {
    id: string;
    source: string;
    status: string;
    error?: string;
    progress?: { stage?: string };
  };
  type Definition = {
    source: string;
    outputs: string[];
    inputs: string[];
    publication?: unknown;
  };
  let jobs = $state<Job[]>([]),
    error = $state(''),
    busy = $state(false);
  let definition = $state<Definition | undefined>();
  let definitionId = $state('');
  let transcript = $state(''),
    playback = $state('');
  let playbackNotice = $state('');
  let player = $state<HTMLAudioElement>();
  let selected = $state<Turn | null>(null);
  let start = $state(0),
    end = $state(0),
    handle = $state(''),
    identity = $state('');
  let correctionBefore = $state(''),
    correctionAfter = $state('');
  let hotwords = $state('');
  let tracks = $state<{ path: string; role: string }[]>([]),
    track = $state('mixed');
  const guidance = $derived(properties(page.text));
  const segments = $derived(turns(transcript));
  const job = $derived(jobs.find((job) => job.source === page.path));
  const attendees = $derived(guidance.attendees || []);
  let suggestions = $state<
    {
      id: string;
      metadata: {
        suggestions?: {
          similarity: number;
          metadata: { reference: { speaker: string; identity: string } };
        }[];
      };
    }[]
  >([]);

  async function run(action: () => Promise<void>) {
    busy = true;
    error = '';
    try {
      await action();
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }
  async function refresh() {
    const manifest = await api.request<{
      derivations: Record<string, Definition>;
    }>('/mcp/artifacts');
    definitionId =
      Object.keys(manifest.derivations).find(
        (id) => manifest.derivations[id].source === page.path
      ) || '';
    definition = Object.values(manifest.derivations).find(
      (d) => d.source === page.path
    );
    jobs = await api.request<Job[]>('/mcp/jobs');
    if (definition?.publication) {
      const path = definition.outputs.find((path) => path.endsWith('.md'));
      if (path) transcript = (await api.page(path)).text;
      const vectors = definition.outputs.find((path) =>
        path.endsWith('.voiceprints.json')
      );
      if (vectors && job?.status === 'current') {
        suggestions = (
          await api.request<{ records: typeof suggestions }>(fileUrl(vectors))
        ).records;
      }
    }
  }
  async function prepareTracks() {
    if (guidance.audio)
      tracks = [{ path: sibling(page.path, guidance.audio), role: 'mixed' }];
    else if (guidance.manifest) {
      const manifestPath = sibling(page.path, guidance.manifest);
      const capture = await api.request<{
        audio: { file?: string; role: string }[];
        container?: { file: string };
      }>(fileUrl(manifestPath));
      tracks = capture.audio.map((a) => ({
        path: sibling(manifestPath, a.file || capture.container!.file),
        role: a.role
      }));
    }
    if (tracks.length) {
      track = tracks[0].role;
      await chooseTrack();
    }
  }
  async function chooseTrack() {
    const source = tracks.find((t) => t.role === track);
    playbackNotice = '';
    playback = '';
    if (source && tracks.filter((t) => t.path === source.path).length > 1) {
      playbackNotice =
        'This capture stores multiple tracks in one container. Browser track selection is unavailable; use the original capture to review track-specific audio.';
      return;
    }
    if (source) playback = fileUrl(source.path);
  }
  onMount(() => {
    let disposed = false;
    hotwords = (guidance.hotwords || []).join(', ');
    void run(async () => {
      await refresh();
      if (!disposed) await prepareTracks();
    });
    let refreshing = false;
    let pending = false;
    const stop = subscribeChanges(() => {
      pending = true;
      if (refreshing) return;
      refreshing = true;
      void (async () => {
        try {
          while (pending && !disposed) {
            pending = false;
            await refresh();
          }
        } catch (e) {
          if (!disposed) error = String(e);
        } finally {
          refreshing = false;
        }
      })();
    });
    return () => {
      disposed = true;
      stop();
    };
  });
  function select(turn: Turn) {
    selected = turn;
    start = turn.start;
    end = turn.end;
    handle = turn.speaker;
    identity = attendees.find((a) => a.handle === handle)?.identity || '';
    if (player) {
      player.currentTime = turn.start;
      void player.play().catch((e) => (error = String(e)));
    }
  }
  async function stage(fields: Record<string, unknown>) {
    await onstage([{ path: page.path, fields }]);
  }
  async function assign() {
    if (!handle.trim() || start < 0 || end <= start)
      throw Error('Choose a speaker and a valid range');
    const next: Attendee[] = structuredClone(attendees);
    // Replace overlapping guidance only within the selected interval, preserving its outside parts.
    for (const attendee of next)
      attendee.ranges = (attendee.ranges || []).flatMap((range) => {
        if (
          (range.track || 'mixed') !== track ||
          range.end <= start ||
          range.start >= end
        )
          return [range];
        return [
          ...(range.start < start ? [{ ...range, end: start }] : []),
          ...(range.end > end ? [{ ...range, start: end }] : [])
        ];
      });
    let attendee = next.find((a) => a.handle === handle.trim());
    if (!attendee) {
      attendee = { handle: handle.trim(), identity, ranges: [] };
      next.push(attendee);
    }
    attendee.identity = identity;
    attendee.ranges!.push({ track, start, end });
    await stage({ attendees: next });
  }
  function inputPaths() {
    return [
      ...new Set([
        ...tracks.map((t) => t.path),
        ...(guidance.manifest ? [sibling(page.path, guidance.manifest)] : [])
      ])
    ];
  }
  async function updateInputs() {
    await prepareTracks();
    await api.request('/mcp/derivations/' + definitionId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ inputs: inputPaths() })
    });
    await refresh();
  }
  async function enable() {
    if (dirty)
      throw Error('Submit the recording document before enabling processing');
    await prepareTracks();
    const base =
      page.path.endsWith('/recording.md') || page.path === 'recording.md'
        ? sibling(page.path, 'transcript.md')
        : page.path.replace(/\.md$/, '.transcript.md');
    await api.request('/mcp/derivations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: page.path,
        recipe: 'speech2md-v1',
        fields: [
          'audio',
          'manifest',
          'title',
          'started_at',
          'ended_at',
          'calendar_event',
          'attendees',
          'edits',
          'hotwords'
        ],
        inputs: inputPaths(),
        outputs: [base, base.replace(/\.md$/, '.voiceprints.json')],
        reference_namespace: 'speakers'
      })
    });
    await refresh();
  }
</script>

<section class="speech-review" aria-label="Recording review">
  <div class="toolbar">
    <strong>{job?.status || 'Not scheduled'}</strong>
    {#if job?.progress?.stage}<span>{job.progress.stage}</span>{/if}
    {#if !definition}<button
        disabled={busy || dirty}
        onclick={() => run(enable)}>Enable transcription</button
      >{/if}
    {#if definition}<button
        onclick={() =>
          onopen(definition!.outputs.find((p) => p.endsWith('.md'))!)}
        >Open transcript</button
      >{/if}
    <button disabled={busy} onclick={() => run(refresh)}>Refresh</button>
    {#if definition}<button
        disabled={busy || dirty}
        onclick={() => run(updateInputs)}>Update source inputs</button
      >{/if}
    {#if job}<button
        disabled={busy || dirty || ['running', 'queued'].includes(job.status)}
        onclick={() =>
          run(async () => {
            await api.request(`/mcp/jobs/${job!.id}/retry`, { method: 'POST' });
            await refresh();
          })}>{job.status === 'failed' ? 'Retry' : 'Regenerate'}</button
      >{/if}
  </div>
  {#if error || job?.error}<p role="alert">{error || job?.error}</p>{/if}
  {#if dirty}<p>
      Review changes are staged locally. Submit them to update the transcript.
    </p>{/if}
  {#if guidance.audio}
    <label
      >Source recording <input
        type="file"
        accept="audio/*,video/*"
        disabled={busy}
        onchange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file)
            void run(async () => {
              await api.request(fileUrl(sibling(page.path, guidance.audio!)), {
                method: 'PUT',
                headers: { 'If-None-Match': '*' },
                body: file
              });
              await prepareTracks();
            });
        }}
      /></label
    >
  {/if}
  {#if tracks.length > 1}<label
      >Track <select bind:value={track} onchange={() => run(chooseTrack)}
        >{#each tracks as source}<option value={source.role}
            >{source.role}</option
          >{/each}</select
      ></label
    >{/if}
  {#if playbackNotice}<p>{playbackNotice}</p>{/if}
  {#if playback}<audio
      controls
      src={playback}
      bind:this={player}
      aria-label="Recording playback"
    ></audio>{/if}
  <details>
    <summary>Hotwords</summary><input
      aria-label="Hotwords"
      bind:value={hotwords}
    /><button
      onclick={() =>
        run(() =>
          stage({
            hotwords: hotwords
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          })
        )}>Stage hotwords</button
    >
  </details>
  <div class="review-columns">
    <div class="turns">
      {#each segments as turn}<button
          class:selected={selected === turn}
          onclick={() => select(turn)}
          ><small
            >{turn.start.toFixed(2)}–{turn.end.toFixed(2)} · {turn.speaker}</small
          ><span>{turn.text}</span></button
        >{/each}
      {#if !segments.length}<p>
          The transcript will appear here after a worker publishes it.
        </p>{/if}
    </div>
    <aside>
      <h3>Speaker guidance</h3>
      <label>Speaker <input bind:value={handle} list="speaker-handles" /></label
      >
      <datalist id="speaker-handles"
        >{#each attendees as attendee}<option value={attendee.handle}
          ></option>{/each}</datalist
      >
      <label
        >Person document <input
          bind:value={identity}
          placeholder="/people/alice.md"
        /></label
      >
      <label
        >Start (seconds) <input
          type="number"
          min="0"
          step="0.01"
          bind:value={start}
        /></label
      >
      <label
        >End (seconds) <input
          type="number"
          min="0"
          step="0.01"
          bind:value={end}
        /></label
      >
      <button
        disabled={!selected}
        onclick={() => {
          end = player?.currentTime || end;
        }}>Split at playhead</button
      >
      <button disabled={busy || !selected} onclick={() => run(assign)}
        >Stage assignment</button
      >
      {#each suggestions.filter((s) => s.id === selected?.speaker) as speaker}
        {#each speaker.metadata.suggestions || [] as suggestion}
          <button
            onclick={() => {
              handle = suggestion.metadata.reference.speaker;
              identity = suggestion.metadata.reference.identity;
            }}
            >{suggestion.metadata.reference.speaker} · {suggestion.similarity.toFixed(
              3
            )}</button
          >
        {/each}
      {/each}
      <details>
        <summary>Text correction</summary>
        <label>Original text <input bind:value={correctionBefore} /></label
        ><label>Correction <input bind:value={correctionAfter} /></label>
        <button
          disabled={!selected || busy}
          onclick={() =>
            run(async () => {
              if (!correctionBefore.trim() || !correctionAfter.trim())
                throw Error('Both correction fields are required');
              await stage({
                edits: [
                  ...(guidance.edits || []),
                  {
                    track,
                    start,
                    end,
                    before: correctionBefore,
                    after: correctionAfter
                  }
                ]
              });
            })}>Stage correction</button
        >
      </details>
      <details>
        <summary>Saved guidance</summary>{#each attendees as attendee}<p>
            {attendee.handle}: {(attendee.ranges || [])
              .map((r) => `${r.track || 'mixed'} ${r.start}–${r.end}`)
              .join(', ')}
          </p>{/each}
      </details>
    </aside>
  </div>
</section>

<style>
  .speech-review {
    padding: 1.5rem;
  }
  .toolbar {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    flex-wrap: wrap;
  }
  audio {
    width: 100%;
    margin-block: 1rem;
  }
  .review-columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(15rem, 22rem);
    gap: 1.5rem;
    margin-top: 1rem;
  }
  .turns button {
    display: block;
    width: 100%;
    text-align: left;
    margin-block: 0.4rem;
    padding: 0.7rem;
  }
  .turns small,
  .turns span {
    display: block;
  }
  .selected {
    outline: 2px solid var(--accent, #276650);
  }
  label {
    display: grid;
    gap: 0.3rem;
    margin-block: 0.6rem;
  }
  input {
    min-width: 0;
  }
  aside button {
    margin-block: 0.3rem;
  }
  [role='alert'] {
    color: #a42b22;
  }
  @media (max-width: 760px) {
    .review-columns {
      grid-template-columns: 1fr;
    }
  }
</style>
