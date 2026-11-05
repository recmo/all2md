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
with the capture entitlement. Home Manager in `recmo/nix`,
[`home/remco/darwin.nix`](https://github.com/recmo/nix/blob/main/home/remco/darwin.nix),
then copies and developer-signs the app **and worker** with identifier
`ventures.wicked.MeetingCapture` and one explicit designated requirement. Its
certificate is `Apple Development: REMCO BLOEMEN (V458N7266F)`; its team is
`LD6C9WL456` (the suffix in the certificate name is not the team ID).

### Test a PR with the installed identity

On the Mac with that certificate and private key in the login keychain, check
out the PR branch, then run from the repository root:

```sh
nix build .#meeting-capture
test_dir="$(mktemp -d /tmp/meeting-capture-test.XXXXXX)"
bash meeting-capture/sign-local.sh \
  result/Applications/MeetingCapture.app "$test_dir/MeetingCapture.app"
```

The helper reads the designated requirement from
`~/Applications/MeetingCapture.app`, signs both the test app and worker with it,
preserves their entitlements, and verifies both against that requirement. It
leaves the installed app untouched. An optional third argument selects another
installed app path. Do not launch the ad-hoc-signed `result` directly for this test.
Apple describes why designated requirements determine whether an updated build
retains its identity in [TN3127](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements).

Quit Meeting Capture from its menu after stopping any recording and waiting for
finalization. To test at the same installed path, keep a backup and swap the
verified build into place (Home Manager marks the installed bundle immutable).
Keep the backup zipped so macOS cannot register a renamed backup as another app:

```sh
installed_app="$HOME/Applications/MeetingCapture.app"
backup_dir="$(mktemp -d "$HOME/meeting-capture-backup.XXXXXX")"
backup_archive="$backup_dir/MeetingCapture.zip"
ditto -c -k --keepParent "$installed_app" "$backup_archive" &&
  unzip -t "$backup_archive" &&
  chflags -R nouchg "$installed_app" &&
  rm -rf "$installed_app" &&
  ditto "$test_dir/MeetingCapture.app" "$installed_app" &&
  open "$installed_app"
```

Check the four permission rows in both the ready and failed-check menus. Each
row should open its own Privacy & Security pane, including when it is checked.
Toggle a permission, return to the menu, and allow a few seconds for its status
to refresh. Accessibility is optional; Documents is checked only when both the
app and worker can write, read, and remove a disposable file in the meeting
folder. A folder failure can also mean disk or filesystem trouble, rather than a
denied TCC grant. A dash means the worker status could not be obtained.

Use **Check recording access again** / **Retry recording check** to run the
full recording and archive test; permission checkmarks alone do not validate
devices or audio. Play audio during that test, then make and stop a short manual
recording. If macOS requests a restart after a permission change, quit and reopen
the app. Restoring toggled permissions is part of cleanup; do not reset TCC to
test identity persistence.
If the recording check fails with permissions available, use **Copy error
details** to capture the failure stage, error domain, code, and underlying error.

After quitting the test app, restore the backup:

```sh
unzip -t "$backup_archive" &&
  chflags -R nouchg "$installed_app" &&
  rm -rf "$installed_app" &&
  ditto -x -k "$backup_archive" "$HOME/Applications" &&
  chflags -R uchg "$installed_app" &&
  open "$installed_app"
```

Avoid running Home Manager activation while the test build occupies its managed
path. The helper does not update Home Manager's deployment marker.

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
This exercises worker folder access and its ffmpeg/ffprobe subprocesses. The
archive check has a 15-second deadline; a timeout stops the worker and its tools
and offers Retry instead of leaving recording disabled indefinitely. The same
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
The menu always lists these four permissions with checkmarks for available
access, empty circles for unavailable access, and dashes for unknown worker
status. Clicking any row opens its corresponding Settings pane. Status refreshes
every three seconds while the menu is open; it never starts an audio recording.
Accessibility trust is queried in a fresh worker, which is the process that
collects metadata. Folder probes delete their disposable files after each check.

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
  Its incoming PCM format determines the audio buffers; `AVAudioFile` writes
  interleaved float PCM to CAF, including when ScreenCaptureKit supplies planar
  samples. The file closes on the delivery queue before finalization reads it.
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
  The menu counts unfinished audio files, not meetings, and offers a Finder link
  to inspect them. Recovery attempts the first pending recording; an unreadable
  file remains pending, so retrying can report the same failure. Recovery errors
  use selectable primary text, with orange reserved for warning icons.
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
