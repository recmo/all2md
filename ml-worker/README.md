# ML worker

`ml-worker` pulls jobs from mdstore and runs one recipe at a time. Apple Silicon
supports transcription, audio enhancement, text embeddings, and reranking. Linux
CPU supports audio enhancement and text models; transcription requires MLX and
is not included in the Linux package. It does not run generative LLM jobs.

| Recipe | Processing | Outputs |
| --- | --- | --- |
| `speech2md-v1` | Existing MOSS transcription/diarization and ReDimNet2 speaker reconciliation | Transcript Markdown and voiceprints |
| `audio-enhancement-dfn3-v1` | DeepFilterNet3 denoising and optional level adjustment | Enhanced audio per role and a capture manifest with provenance |
| `audio-levels-v1` | Level adjustment without denoising | Adjusted audio per role and a capture manifest with provenance |
| `text-embeddings-zembed-1-v1` | ZeroEntropy zembed-1 (4B) | Normalized 2,560-dimensional vectors |
| `text-rerank-zerank-2-v1` | ZeroEntropy zerank-2 (4B) | Ranked candidate IDs, input indices, and relevance scores |

The runner advertises locally installed capabilities and claims only matching
recipes. Selected text models load before the first claim and stay resident together
in a credential-free inference process across jobs, including while idle. Lease
loss or interruption kills the inference process; the next text job reloads the
selected models. Ordinary recipe errors retain the loaded models. Audio and
transcription recipes use separate child processes; cancellation kills their
process groups, including audio tools. Inputs are verified by SHA-256, outputs
must match the assignment, and mdstore fences stale attempts and validates atomic
publication. Worker credentials are removed from the inference subprocess.
Workspaces are isolated by server, job, and fingerprint. Starting a new fingerprint
removes that job's obsolete workspaces under its lock. The MOSS cache is shared
across a job’s fingerprints and validated by speech2md before reuse, so speaker
corrections can replay compatible generations. A file lock prevents two
attempts from concurrently using a job's workspace, including a child whose parent
crashed. Disposable enhancement waveforms are cleaned up after completion or
cancellation, and stale scratch is removed before retrying. Failed attempts cannot
publish old outputs left in the cache.

## Run

On Apple Silicon or Linux, with Nix and the same separate worker credential
configured on mdstore and in `MDSTORE_WORKER_TOKEN`:

```sh
nix run .#ml-worker -- --list-recipes
nix run .#ml-worker -- --server https://your-mdstore-host
# Claim at most one matching job, then exit:
nix run .#ml-worker -- --server https://your-mdstore-host --once \
  --recipes audio-enhancement-dfn3-v1 text-embeddings-zembed-1-v1 text-rerank-zerank-2-v1
```

Idle workers poll once a second; failed requests retry after ten seconds.

`--cache` defaults to `~/.cache/ml-worker`. `--device auto` chooses PyTorch MPS
when available, otherwise CPU; `--device cpu` forces CPU for text models.
Text models use FP32 on CPU and BF16 on MPS. Allow roughly 32 GiB for both CPU
text models together, plus inference memory; `OMP_NUM_THREADS=4` can limit CPU
parallelism.
DeepFilterNet uses its native Rust/CPU runtime. Nix packages the dependencies and
embeds the DeepFilterNet3 model. The two text models download pinned weights into
the Hugging Face cache on first startup; loading disables remote Python code and
requires safetensors. No packages or toolchains are installed at runtime.

See [Zawrat CPU measurements](benchmarks.md) before scheduling the 4B text models
on a CPU. These queued recipes do not provide synchronous mdstore retrieval.

Use `--recipes speech2md-v1` for a transcription-only worker.
Use `ml-worker` for the additional recipes; its transcription recipe invokes the
existing speech2md CLI and retains leased speaker-reference matching.

## Declare jobs

As with [transcription](../mdstore/README.md#assets-and-derived-documents),
add declarations under `derivations` in `config.yaml`. Recipe parameters live in
the authored source document's `ml` frontmatter mapping. Select `fields: [ml]` so
mdstore fingerprints those parameters and sends them in the assignment's frozen
`parameters` projection. All paths below are repository-relative, except files
inside the generated capture manifest, which are relative to that manifest.

### Audio enhancement for playback and model input

An example source document, `jobs/audio.md`:

```yaml
---
ml:
  denoise: true
  levels: true
  attenuation_db: 18
  tracks:
    - input: meetings/capture.mka
      stream_index: 0
      role: microphone
      output: meetings/microphone.enhanced.m4a
    - input: meetings/capture.mka
      stream_index: 1
      role: participants
      output: meetings/participants.enhanced.m4a
---
# Enhance a Meeting Capture recording
```

Its job declaration:

```yaml
derivations:
  enhance-meeting:
    source: jobs/audio.md
    recipe: audio-enhancement-dfn3-v1
    fields: [ml]
    inputs: [meetings/capture.mka]
    outputs:
      - meetings/microphone.enhanced.m4a
      - meetings/participants.enhanced.m4a
      - meetings/enhanced-capture.json
```

Upload the input through mdstore's normal file API before configuring the job.
Stream indices refer to audio streams in the file; Meeting Capture v2 manifests
identify the microphone and participant streams. Each role must be unique.
The output set must contain exactly the per-track files and one JSON manifest.
For level adjustment alone, select `audio-levels-v1` and omit `denoise` or set it
to `false`. For denoising alone, use `audio-enhancement-dfn3-v1` with `levels: false`.
At least one stage must be enabled. `attenuation_db` limits suppression to 0–36 dB.

The worker decodes each stream to 48 kHz PCM, denoises before level adjustment,
and compensates DeepFilterNet's processing delay. Padding protects the final
speech; it is trimmed after inference to retain the original sample count.
Microphone and participant roles remain separate, without silence removal,
VAD gating, source separation, or time compression.

Level adjustment measures track RMS and sample peaks, targets -23 dBFS RMS,
caps amplification at 12 dB, and leaves 1 dB of sample-peak headroom. Silent or
near-silent tracks below -60 dBFS RMS are not amplified. This is bounded constant
gain, not dynamic compression or a guarantee of perceived loudness. The manifest
records measured levels, applied gain, source hashes, parameters, and model version.

`.wav` and `.flac` outputs are lossless. `.m4a` uses AAC at 128 kb/s for convenient
browser playback and smaller transfers; it adds an encoding generation. Keep
originals unchanged. mdstore currently limits each worker output to 64 MiB;
choose an appropriate duration/format or split long recordings into jobs.

The generated manifest follows the existing Meeting Capture v1 per-file track
contract. A separate recording document can reference it with
`manifest: enhanced-capture.json`; assign the manifest and **all its audio files**
as inputs to its `speech2md-v1` job. This makes enhancement available as model
preprocessing without changing the original recording. Choose original or enhanced
inputs explicitly and compare transcript and speaker-match quality. MOSS and
ReDimNet2 do not require external denoising in their documented inference paths.

Enhanced files can be opened with the existing WebUI file player; an integrated
Original/Enhanced selector is not implemented.
The enhancement stage does not recover Zoom/Meet's original encoded packets or
apply decoder-side codec restoration to those packets.

### Text embeddings

Source document, `jobs/embed.md`:

```yaml
---
ml:
  namespace: documents
  input_type: document
  texts:
    - id: notes/meeting.md#decision
      text: We agreed to ship the release on Friday.
    - id: notes/design.md#storage
      text: The repository stores immutable, content-addressed assets.
---
# Embed selected passages
```

```yaml
derivations:
  embed-passages:
    source: jobs/embed.md
    recipe: text-embeddings-zembed-1-v1
    fields: [ml]
    inputs: []
    outputs: [vectors/passages.json]
```

The model is
[`zeroentropy/zembed-1-embedding`](https://huggingface.co/zeroentropy/zembed-1-embedding),
pinned to `cf13c81f3274394053d166740294f7eea4586f7a`. The worker uses the
model's query/document ChatML prefixes and closing suffix, pools the last token,
and L2-normalizes the 2,560-dimensional results. These conventions match the
pinned Sentence Transformers configuration without executing its custom Python.
Embedding-space identity includes the recipe and namespace; text vectors cannot
use the `speakers` namespace. The new recipe cannot mix with old E5 artifacts.
Queries and documents must use the same local recipe and revision. Mdstore has
no hosted ZeroEntropy API integration or fallback.

Outputs use mdstore's existing `space`/`records` vector artifact format, with
record IDs, original text, input mode, model revision, and source provenance.
Explicit derivations can participate in frozen references through
`reference_namespace` and leased vector search. Author their passages in `ml.texts`;
arbitrary source-body edits are not inputs to an explicit derivation.

Mdstore also queues its own search chunks automatically and sends query embeddings
and reranking through these same recipes. Workers claim these retrieval assignments
through `/worker` and return their JSON inline in completion. Mdstore validates the
pinned model, revision, input IDs, dimensions, normalization, and lease before
installing document vectors into its sidecars and live search index. Retrieval
assignments do not publish repository artifacts or create Git commits. Each attempt
uses an isolated temporary workspace that is removed when the attempt ends. Workers
claim the next job immediately after processing one and wait only when idle or after an error. See
[mdstore](../mdstore/README.md) for configuration and migration.

### Reranking

Source document, `jobs/rank.md`:

```yaml
---
ml:
  query: When is the release?
  top_n: 1
  texts:
    - id: decision
      text: We agreed to ship the release on Friday.
    - id: storage
      text: Assets are content-addressed.
---
# Rank search candidates
```

```yaml
derivations:
  rank-passages:
    source: jobs/rank.md
    recipe: text-rerank-zerank-2-v1
    fields: [ml]
    inputs: []
    outputs: [results/ranked.json]
```

The model is
[`zeroentropy/zerank-2-reranker`](https://huggingface.co/zeroentropy/zerank-2-reranker),
pinned to `5eae30d5ee3c6b2df2ef6d723bde45172d761c4c`. Each query/passage pair
uses its pinned chat template and the raw next-token `Yes` logit, matching the
model's current LogitScore configuration. Results preserve original indices and
IDs, sort by descending score, and break ties by original order. Scores are not
probabilities. This is a queued batch job, not a synchronous search-time service.

Both models have 4B parameters and Apache-2.0 weights. They download on first use
(roughly 8 GB per model on disk). Both stay loaded while jobs run one at a time,
using FP32 on CPU and BF16 on MPS. MPS requires a Mac/PyTorch combination with
BF16 support (macOS 14+).
Allow memory beyond the weights for inference; long passages use more memory.

Both text recipes accept 1–256 records with unique nonempty string IDs and
nonempty text of at most 32,768 characters. The formatted input must fit within
32,768 model tokens (query and passage combined for reranking). Oversize inputs
fail rather than silently truncating; split long documents into passages.
Unknown parameters and non-finite model outputs fail the job.

## Development and remaining work

```sh
nix develop .#ml-worker
PYTHONPATH=ml-worker/src python -m pytest ml-worker/tests
uv sync --project ml-worker --extra dev
PYTHONPATH=ml-worker/src uv run --project ml-worker pytest ml-worker/tests
nix build .#checks.x86_64-linux.ml-worker
```

Fixture checks exercise real HTTP transfers, subprocess execution, checksum
failure, lease loss, publication, gain control, and DeepFilterNet audio alignment.
Tensor tests verify pooling, prefixes, and ranking without downloading weights
when PyTorch is installed. Actual quality needs representative Zoom/Meet audio,
reviewed transcripts and speaker identities, and relevance judgments for text.
MacBook MPS performance and real-recording quality are not established by these
checks. OCR integration, codec restoration, launchd deployment, automatic corpus
embedding jobs, and the WebUI enhancement selector remain separate work.
