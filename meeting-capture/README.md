# Meeting Capture

Meeting Capture is a local-only macOS menu-bar recorder. It watches Core Audio
for another process opening a microphone, offers a ten-second veto, and records
the local microphone and that process's output as separate tracks.

Records are written to `~/Documents/Meetings/YYYY/MM/`. The canonical archive
is one audio-only Matroska (`.mka`) file with independently selectable Opus
streams; a versioned JSON manifest carries capture provenance for `speech2md`.

## Development

```sh
nix develop .#meeting-capture
cd meeting-capture
xcodegen generate
xcodebuild -project MeetingCapture.xcodeproj -scheme MeetingCapture build
```

To build the installable app through Nix:

```sh
nix build .#meeting-capture
open result/Applications/MeetingCapture.app
```

The Nix derivation uses the locally installed Xcode and ad-hoc signs the result
with the capture entitlement.

At launch, Meeting Capture checks Microphone and Screen & System Audio
Recording access, requesting authorization if needed, and tests writing to the
meeting folder. It then opens the real capture paths in the recording app for a
1.5-second disposable recording before enabling monitoring or manual recording.
The test requires saved microphone frames (silence is valid), successful system
capture start/stop, and readable system audio when samples arrive. On a quiet
system, the menu explicitly reports that access was authorized but no system
audio was observed; play audio and rerun the check to verify sample delivery.
Test audio is deleted and never appears among meetings or recovery files.

The startup check also asks the actual worker to read the disposable audio and
create, verify, hash, rename, and delete a small archive under the meeting folder.
This exercises worker folder access and its ffmpeg/ffprobe subprocesses. The same
worker checks Accessibility trust without prompting and, when authorized, tries
an Accessibility read and observer creation against the app. The menu reports
the optional metadata check separately; a target meeting app can still expose
different Accessibility capabilities.

The permission inventory is Microphone, Screen & System Audio Recording,
Documents access under Files & Folders, and optional Accessibility. The current
implementation does not use Camera, Input Monitoring, Automation/Apple Events,
Calendar, Contacts, or notification authorization, and does not require Full
Disk Access. The audio-input signing entitlement is included in the app package;
the real microphone probe exercises that capability as well as user consent.

Failures keep recording disabled and expose Settings links and an explicit Retry
action. macOS may require quitting and reopening the installed app after a grant.
Permission rechecks cannot run during capture, countdown, or detection. Optional
Accessibility metadata does not gate recording or prompt automatically.
Automatic recording never falls back to microphone-only capture when participant
audio is unavailable. The startup check exercises default devices; it cannot
guarantee that permissions or a meeting application's devices will remain usable.

## Current capture path

- Core Audio process objects drive automatic detection. Browser helper
  processes are resolved to their owning application, with default-input device
  activity as the unattributed fallback.
- For attributed clients, the process object's input-scoped device list identifies
  the microphone selected by the meeting application, even when it is not the
  macOS default. The active device is shown in the menu and recorded as a
  `microphoneDevice` metadata event.
- Two seconds of sustained activity opens a ten-second, vetoable countdown.
- The microphone is captured to crash-safe PCM CAF segments in each device's
  native format. If the meeting application changes input devices, capture
  follows it with a new segment; the short restart interval is recorded as an
  interruption. Participant audio uses an
  application-filtered ScreenCaptureKit stream and excludes this application.
- Capture does no live transcoding. After stop, finalization aligns the native
  segments and creates one `.mka` without a meeting mix: microphone is mono
  Opus at 96 kb/s VBR and participants remain stereo Opus at 128 kb/s VBR.
  Both use 48 kHz, the Opus audio application, 20 ms frames, complexity 10,
  and no DTX.
- Finalization probes the stream contract, decodes every stream, atomically
  publishes the archive, records its SHA-256 in the v2 manifest, and only then
  deletes the temporary PCM files. A failed finalization leaves the PCM files
  available for recovery.
- Finalization, verification, hashing, manifest publication, interrupted-file
  recovery, and optional Accessibility probing run in dedicated worker
  processes. A worker crash or hang cannot terminate the recorder or replace
  its live state. Stopping a recording returns the app to monitoring before
  deferred finalization finishes.
- Interrupted CAF chunks are discovered at launch and can be recovered from
  the menu only while idle. Recovery remains a secondary maintenance status;
  it cannot replace detection, countdown, or recording state. Header-only CAF
  files are preserved with an `.unrecoverable.caf` suffix rather than retried
  forever.
- Capture, finalization, and recovery claim each recording exclusively. Claimed
  recordings are excluded from recovery discovery; worker claims are released by
  the operating system even if a worker crashes. Unreadable audio fails processing
  and is preserved for recovery rather than silently omitted and deleted.
- Manually started recordings, including those selected from active audio
  applications, continue until explicitly stopped even when the meeting is muted.
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

No countdown audio is persisted. A skipped trigger therefore leaves no audio
on disk. A bounded in-memory pre-roll and a Core Audio process-tap-first path
remain candidates for the real-meeting validation cycle; neither changes the
manifest contract.

## Milestone 1 acceptance

Schema v2 remains provisional until recordings cover at least five meetings,
three hours, Zoom and two other applications. The validation pass must also
confirm isolated participant audio, interrupted-recording recovery, false
trigger rate, HUD behavior, and audio quality. Zoom, Brave/Google Meet,
FaceTime, Slack, and Signal Accessibility trees should be evaluated during
those sessions before adding any per-application parser.
