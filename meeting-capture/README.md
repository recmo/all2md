# Meeting Capture

Meeting Capture is a local-only macOS menu-bar recorder. It watches Core Audio
for another process opening a microphone, offers a ten-second veto, and records
the local microphone and that process's output as separate lossless tracks.

Records are written to `~/Documents/Meetings/YYYY/MM/`. FLAC audio is the
canonical source; a versioned JSON manifest carries capture provenance for the
future `speech2md` processor.

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

The first automatic recording requests Microphone and Screen & System Audio
Recording permissions. Automatic recording does not fall back to a
microphone-only capture when participant audio is unavailable; the menu opens
the correct System Settings pane so permission can be repaired. Accessibility
is optional and only enriches metadata.

## Current capture path

- Core Audio process objects drive automatic detection. Browser helper
  processes are resolved to their owning application, with default-input device
  activity as the unattributed fallback.
- Two seconds of sustained activity opens a ten-second, vetoable countdown.
- The microphone is recorded as PCM CAF. Participant audio currently uses an
  application-filtered ScreenCaptureKit stream and excludes this application.
- Finalization converts both tracks to FLAC, records SHA-256 checksums, writes
  the v1 manifest atomically, and only then deletes temporary CAF chunks.
- Interrupted CAF chunks are discovered at launch and can be recovered from
  the menu.
- Generic Accessibility inspection currently contributes the focused window
  title when permission is available; it never gates recording.

No countdown audio is persisted. A skipped trigger therefore leaves no audio
on disk. A bounded in-memory pre-roll and a Core Audio process-tap-first path
remain candidates for the real-meeting validation cycle; neither changes the
manifest contract.

## Milestone 1 acceptance

Schema v1 remains provisional until recordings cover at least five meetings,
three hours, Zoom and two other applications. The validation pass must also
confirm isolated participant audio, interrupted-recording recovery, false
trigger rate, HUD behavior, and audio quality. Zoom, Brave/Google Meet,
FaceTime, Slack, and Signal Accessibility trees should be evaluated during
those sessions before adding any per-application parser.
