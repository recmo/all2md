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

The first launch requests Microphone and Screen & System Audio Recording
permissions. Accessibility is optional and only enriches metadata.

## Current capture path

- Core Audio process objects drive automatic detection, with default-input
  device activity as the unattributed fallback.
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
- Interrupted CAF chunks are discovered at launch and can be recovered from
  the menu.
- Generic Accessibility inspection currently contributes the focused window
  title when permission is available; it never gates recording.

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
