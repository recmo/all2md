# Meeting Capture

Meeting Capture is a local-only macOS menu-bar recorder. It watches Core Audio
for another process opening a microphone, offers a ten-second veto, and records
the microphone plus every process producing audio into separate tracks. This
includes unrelated applications playing during the recording. Core Audio process
taps work without an application window, including a FaceTime call bubble.
Browser and application helpers keep their own process tracks.

Records are written to `~/Documents/Meetings/YYYY/MM/`. The canonical archive
is one audio-only Matroska (`.mka`) file with independently selectable Opus
streams; a versioned JSON manifest carries capture provenance for `speech2md`.

## Build, test, and install locally

Quit Meeting Capture, then run this one command from the repository root:

```sh
bash meeting-capture/install-local.sh
```

This builds the Nix package, runs the macOS XCTest suite in the Nix development
shell, and signs both the app and worker using the installed app's designated
requirement. Only after those checks pass does it replace
`~/Applications/MeetingCapture.app` and reopen it. It creates no backups and
refuses to replace a running app. The app also refuses Quit while recording or
starting/stopping capture. The `result` link retains the Nix runtime dependencies.

Signing follows
[`recmo/nix/home/remco/darwin.nix`](https://github.com/recmo/nix/blob/main/home/remco/darwin.nix):
identifier `ventures.wicked.MeetingCapture`, certificate
`Apple Development: REMCO BLOEMEN (V458N7266F)`, team `LD6C9WL456`. The
certificate and private key must already be in the login keychain, and the
installed developer-signed app must exist. `sign-local.sh` copies its exact
requirement, preserves entitlements, and verifies both signed executables.
The Nix package itself is ad-hoc signed; launch the installed copy for permission
testing. Home Manager's deployment marker is left unchanged.

For an interactive development shell:

```sh
nix develop .#meeting-capture --command bash --noprofile --norc
bash meeting-capture/test-local.sh
```

The test script builds the app and test bundle, then runs XCTest directly. It
works for headless CI accounts without a GUI `testmanagerd` session. Live audio
capture is opt-in; ordinary tests do not request recording permissions.

Check each permission row opens its own Settings pane, toggle access, return to
the menu, and retry the recording check. If macOS requests a restart, quit and
reopen the app. Use **Copy error details** on failure. With audio playing, make
an attributed manual recording and check the separate microphone and participant
tracks. Also verify Skip/Stop suppress the current microphone session, input
device/format changes create a new segment, and interrupted recordings can be
selected independently for recovery. Raw Accessibility traces remain enabled
for future speaker attribution.

At launch, Meeting Capture requests Microphone access, opens a Core Audio tap
(which prompts for System Audio Recording access when needed), and tests writing
to the meeting folder. It opens the real capture paths in the recording app
for a 1.5-second disposable recording before enabling monitoring or manual
recording.
The test requires saved microphone frames (silence is valid), successful
process-tap start/stop, and readable system audio when samples arrive. On a quiet
system, the menu explicitly reports that access was authorized but no system
audio was observed; play audio and rerun the check to verify sample delivery.
Test audio is deleted and never appears among meetings or recovery files.

The startup check also asks the actual worker to read the disposable audio and
create, verify, hash, rename, and delete a small archive under the meeting folder.
This exercises worker folder access and its ffmpeg/ffprobe subprocesses. The
archive check has a 15-second deadline; a timeout stops the worker and its tools
and offers Retry instead of leaving recording disabled indefinitely. The same
worker checks Accessibility trust without prompting and, when authorized, tries
an Accessibility read and observer creation against the app. The menu reports
the optional metadata check separately; a target meeting app can still expose
different Accessibility capabilities.

The permission inventory is Microphone, System Audio Recording,
Documents access under Files & Folders, and optional Accessibility. The current
implementation does not use Camera, Input Monitoring, Automation/Apple Events,
Calendar, Contacts, or notification authorization, and does not require Full
Disk Access. The audio-input signing entitlement is included in the app package;
the real microphone probe exercises that capability as well as user consent.
The menu always lists these four permissions with checkmarks for available
access, empty circles for unavailable access, and dashes for unknown status.
Clicking any row opens its corresponding Settings pane. Microphone, folder, and
Accessibility status refresh when the menu opens and the app becomes active;
folder probes run off the UI thread. System audio access reflects the last recording check because taps have no public
preflight API; rerun the check after changing System Settings. Screen recording
permission and a connected display are not required. Status refreshes never
start an audio recording.
Accessibility trust is queried in a fresh worker, which is the process that
collects metadata. Folder probes delete their disposable files after each check.

Failures keep recording disabled and expose Settings links and an explicit Retry
action. macOS may require quitting and reopening the installed app after a grant.
Permission rechecks cannot run during capture, countdown, or detection. Optional
Accessibility metadata does not gate recording or prompt automatically.
A process capture failure stops recording visibly rather than silently dropping
that process. A quiet system can produce a complete microphone-only archive.
The startup check exercises default devices; it cannot
guarantee that permissions or a meeting application's devices will remain usable.

The v3 manifest records a unique `id` and `process` metadata (raw PID, HAL object
ID, bundle identifier when available, and process name) for every process stream.
Matroska stream titles include the name and PID. The session journal saves every
process file and its start time before capture, so finalization and interrupted
recovery preserve isolation and align late-starting tracks to the microphone.
`speech2md` uses `process-<track UUID>` as each source identity, preventing tracks
from sharing speaker labels, cache entries, or review hints. Older archived v1/v2
manifests remain readable by `speech2md`; old interrupted journals are preserved
for manual salvage rather than guessed into the new format.

## Current capture path

- Core Audio process objects drive automatic detection. Browser helper
  processes are resolved to their owning application. Unattributed device activity
  never triggers automatic recording, so the recorder cannot trigger itself.
- For attributed clients, the process object's input-scoped device list identifies
  the microphone selected by the meeting application, even when it is not the
  macOS default. The active device is shown in the menu and recorded as a
  `microphoneDevice` metadata event.
- Two seconds of sustained activity opens a ten-second, vetoable countdown.
  Skip and Stop suppress that process until its microphone activity ends.
- The microphone is captured to crash-safe PCM CAF segments in each device's
  native format. Device changes and AVAudioEngine configuration changes restart
  the microphone into a new segment; the short restart interval is recorded as an
  interruption. Application audio uses one private, unmuted Core Audio tap per
  raw output process, excluding this recorder. New output processes are discovered
  every 250 ms; very short-lived sounds can precede discovery. Quiet processes
  retain their tap, preserving gaps when they resume. Exited processes release
  their tap and retain their recorded file. Each new process lifetime gets a
  separate track UUID, even if macOS reuses a PID or HAL object ID.
  Both capture paths share a timestamp-aware PCM writer. The incoming format
  determines the audio buffers; `AVAudioFile` writes
  interleaved float PCM to CAF, including when the tap supplies planar samples.
  Sample timestamps determine the file position: missing time is filled
  with bounded silence buffers, and duplicate/overlapping frames are trimmed.
  Per-track start times align process audio to the session start. The file closes
  after callbacks stop and the delivery queue drains, before finalization reads it.
  Capture failures stop
  recording visibly and produce an incomplete archive, even if audio files exist.
- Capture does no live transcoding. After stop, finalization aligns the native
  segments and creates one `.mka` without a meeting mix: microphone is mono
  Opus at 96 kb/s VBR and each process is stereo Opus at 128 kb/s VBR.
  All tracks use 48 kHz, the Opus audio application, 20 ms frames, complexity 10,
  and no DTX.
- Finalization probes the stream contract, decodes every stream, atomically
  publishes the archive, records its SHA-256 in the v3 manifest, and only then
  deletes the temporary PCM files. A failed finalization leaves the PCM files
  available for recovery.
- Finalization, verification, hashing, manifest publication, interrupted-file
  recovery, and optional Accessibility probing run in dedicated worker
  processes. A worker crash or hang cannot terminate the recorder or replace
  its live state. Stopping a recording returns the app to monitoring before
  deferred finalization finishes.
- Accessibility workers exit when their recorder's lifetime pipe closes, even
  if an Accessibility call is stuck. On normal stop, the recorder requests a
  final snapshot and closes the pipe after a two-second grace period. No worker
  PID is handed to finalization. Recovery waits for the metadata writer's
  file lock before publishing and checksumming its log. Worker request files
  are removed after loading; normal-exit cleanup also outlives the app's handle.
- Interrupted CAF chunks are discovered at launch and can be recovered from
  the menu while idle or after a recording check or capture error. Recovery does
  not require a working microphone or recording authorization. It remains a
  secondary maintenance status;
  it cannot replace detection, countdown, or recording state. Header-only CAF
  files are preserved with an `.unrecoverable.caf` suffix rather than retried
  forever.
- Capture, finalization, and recovery claim each recording exclusively. Claimed
  recordings are excluded from recovery discovery; worker claims are released by
  the operating system even if a worker crashes. Unreadable audio fails processing
  and is preserved for recovery rather than silently omitted and deleted.
  The menu groups unfinished files by recording and offers Recover and Show files
  for each one. A damaged recording does not block selecting another. An atomic,
  synchronized session journal records segment start times, metadata, failures,
  and interruptions; normal finalization and recovery use the same record.
  Recovery derives saved segment lengths from the audio while preserving their
  recorded offsets. Old files without a journal are kept for manual salvage
  rather than guessed into a potentially misaligned archive. Recovery errors use
  selectable primary text, with orange reserved for warning icons.
- Manually started recordings capture all output processes and continue until explicitly stopped even when the meeting is muted.
- Generic Accessibility inspection currently contributes the focused window
  title when permission is available; it never gates recording.
- Every attributed recording also starts a generic Accessibility probe for the
  same process. Beside the `.mka` it writes a checksummed
  `*-accessibility.jsonl` sidecar containing an initial tree snapshot, raw AX
  notifications, attribute-level tree diffs, periodic fallback rescans, and a
  final diff. A crash leaves the append-only `.part.jsonl` recoverable. Probe
  logs may contain participant names, captions, and other visible interface
  text; they never leave the Mac automatically. Missing Accessibility permission
  becomes a manifest warning and never blocks audio capture.

For local acceptance, record a FaceTime call with only the call bubble visible,
verify its audio and the microphone as separate tracks, then repeat with the
full FaceTime window open. Play audio in a browser too and verify a third track.
Start another app during capture and verify its track begins at the right offset.
Check that quiet/resumed processes keep their tracks, process exit preserves saved
audio, remote silence
does not stop a manually started recording, and repeated start/stop cycles leave
no aggregate devices behind. Test permission denial and a retry after granting
System Audio Recording access. These checks require the installed, signed app
on macOS. The optional windowless tone test exercises real process-tap delivery
and two start/stop cycles:

```sh
MEETING_CAPTURE_TEST_PROCESS_TAP=1 nix develop .#meeting-capture --command \
  bash meeting-capture/test-local.sh
```

No countdown audio is persisted. A skipped trigger therefore leaves no audio
on disk. A bounded in-memory pre-roll remains a candidate for the real-meeting
validation cycle and would preserve the manifest contract.

## Milestone 1 acceptance

Schema v3 remains provisional until recordings cover at least five meetings,
three hours, Zoom and two other applications. The validation pass must also
confirm isolated process audio, interrupted-recording recovery, false
trigger rate, HUD behavior, and audio quality. Zoom, Brave/Google Meet,
FaceTime, Slack, and Signal Accessibility trees should be evaluated during
those sessions before adding any per-application parser.
