<script lang="ts">
  import { untrack } from 'svelte';
  import StagedDiff from './StagedDiff.svelte';
  import { mergeParts, type Conflict } from './reconcile';
  let {
    path,
    conflict,
    disabled,
    onresolve,
    onprogress
  }: {
    path: string;
    conflict: Conflict;
    disabled: boolean;
    onresolve: (text: string | null) => void;
    onprogress: (choices: Record<number, string>) => void;
  } = $props();
  const parts = $derived(
    conflict.base !== null && conflict.local !== null && conflict.server.exists
      ? mergeParts(conflict.base, conflict.local, conflict.server.text)
      : null
  );
  let choices = $state<Record<number, string>>(
    untrack(() => ({ ...conflict.choices }))
  );
  let manual = $state<Record<number, boolean>>({});
  const complete = $derived(
    parts?.every((part, i) => 'text' in part || i in choices)
  );
  function choose(i: number, text: string) {
    choices = { ...choices, [i]: text };
    onprogress(choices);
  }
</script>

<section class="merge-conflict" aria-label={`Conflict in ${path}`}>
  <h3>{path}</h3>
  {#if parts}
    <p>
      Choose a resolution for each overlapping passage. Other changes are merged
      automatically.
    </p>
    {#each parts as part, i}
      {#if !('text' in part)}
        <div class="conflict-passage">
          <h4>Conflicting passage</h4>
          <StagedDiff {path} before={part.yours} after={part.server} />
          <p class="muted">
            Removed lines are yours; added lines are the server version.
          </p>
          <div class="submit-actions">
            <button
              {disabled}
              onclick={() => {
                manual[i] = false;
                choose(i, part.yours);
              }}>Yours</button
            >
            <button
              {disabled}
              onclick={() => {
                manual[i] = false;
                choose(i, part.server);
              }}>Server</button
            >
            <button
              {disabled}
              onclick={() => {
                manual[i] = true;
                choose(i, choices[i] ?? part.yours);
              }}>Edit manually</button
            >
          </div>
          {#if manual[i]}
            <label for={`merge-${path}-${i}`}>Resolved passage</label>
            <textarea
              id={`merge-${path}-${i}`}
              rows="8"
              value={choices[i]}
              {disabled}
              oninput={(e) => choose(i, e.currentTarget.value)}></textarea>
          {:else if i in choices}<pre class="merge-choice">{choices[i] ||
                '(Passage removed)'}</pre>{/if}
        </div>
      {/if}
    {/each}
    <button
      disabled={disabled || !complete}
      onclick={() =>
        onresolve(
          parts!
            .map((part, i) => ('text' in part ? part.text : choices[i]))
            .join('')
        )}>Apply resolution</button
    >
  {:else}
    <p>
      {conflict.local === null
        ? 'You staged a deletion, but this file changed on the server. If this is part of a move, keeping the server version leaves the destination staged as a copy.'
        : !conflict.server.exists
          ? 'This file was deleted on the server. Restore your draft or accept the deletion.'
          : 'A file was created at this path on both sides. Choose which version to keep.'}
    </p>
    <StagedDiff
      {path}
      before={conflict.local}
      after={conflict.server.exists ? conflict.server.text : null}
    />
    <div class="submit-actions">
      {#if conflict.local !== null && conflict.server.exists}
        <button
          {disabled}
          onclick={() => {
            manual[0] = true;
            choose(0, choices[0] ?? conflict.local!);
          }}>Edit manually</button
        >
      {/if}
      <button {disabled} onclick={() => onresolve(conflict.local)}
        >{conflict.local === null
          ? 'Delete changed file'
          : !conflict.server.exists
            ? 'Restore as new file'
            : 'Keep yours'}</button
      >
      <button
        {disabled}
        onclick={() =>
          onresolve(conflict.server.exists ? conflict.server.text : null)}
        >{conflict.server.exists
          ? 'Keep server version'
          : 'Accept deletion'}</button
      >
    </div>
    {#if conflict.local !== null && conflict.server.exists && (manual[0] || 0 in choices)}
      <label for={`merge-${path}`}>Resolved document</label>
      <textarea
        id={`merge-${path}`}
        rows="12"
        value={choices[0]}
        {disabled}
        oninput={(e) => choose(0, e.currentTarget.value)}></textarea>
      <button {disabled} onclick={() => onresolve(choices[0])}
        >Apply resolution</button
      >
    {/if}
  {/if}
</section>
