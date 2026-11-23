# Meeting Capture

Meeting Capture is a local-only macOS menu-bar recorder. It starts when another
application uses a microphone and captures the microphone plus every process
producing audio as separate tracks. This includes unrelated applications playing
during a meeting. Private, unmuted Core Audio process taps work without a window,
including a FaceTime call bubble. Browser helpers retain their own process tracks.
Manual recordings continue until explicitly stopped.

Recordings live in `~/Documents/Meetings/YYYY/MM/`: one audio-only Matroska
(`.mka`) archive, a version 3 JSON manifest, and optional window-title metadata.
`speech2md` uses each process track's UUID to isolate source identities.

## Build, test, and install

On a Mac, choose **Disable and Quit**, then run from the repository root:

```sh
bash meeting-capture/install-local.sh
```

The installer builds the Nix package, runs XCTest in the Nix development shell,
and signs the app and worker using the installed app's designated requirement.
After those checks pass it replaces `~/Applications/MeetingCapture.app` and
reopens it. It refuses to replace a running app and atomically exchanges the
verified staged bundle with the installed bundle. The `result` link retains Nix
runtime dependencies. The app refuses Quit during capture startup, recording,
and stopping. Interrupted background processing preserves its original audio.

Signing follows
[`recmo/nix/home/remco/darwin.nix`](https://github.com/recmo/nix/blob/main/home/remco/darwin.nix):
identifier `ventures.wicked.MeetingCapture`, certificate
`Apple Development: REMCO BLOEMEN (V458N7266F)`, team `LD6C9WL456`.
The certificate, private key, and existing developer-signed app must be present.
The Nix package is ad-hoc signed; use the installed copy for permission testing.

For development:

```sh
nix develop .#meeting-capture --command bash --noprofile --norc
bash meeting-capture/test-local.sh
```

`project.yml` defines the app, worker, and tests. The checked-in Xcode project
includes the same source files; regenerate it with `xcodegen generate` inside
`meeting-capture` when changing build settings. The test script builds the app
and test bundle and runs XCTest without a GUI testmanager session. The GitHub
Actions workflow builds and verifies the release package on macOS 15 and macOS 26,
then runs this suite against its packaged worker. Set `MEETING_CAPTURE_TEST_APP`
to an app bundle to exercise another built worker locally. Live audio is opt-in:

```sh
MEETING_CAPTURE_TEST_PROCESS_TAP=1 nix develop .#meeting-capture --command \
  bash meeting-capture/test-local.sh
```

## Capture and failure handling

One serial control queue owns the recording session, native devices, and timing
journal. The UI receives immutable status snapshots. Device setup, file flushing,
and journal writes do not run on the UI thread. The menu distinguishes starting,
recording, and stopping; archive processing is a separate background activity.

Core Audio microphone activity triggers recording on a 500 ms poll. Browser
helpers are resolved to their owning application for detection, and the
application's input-device list identifies its selected microphone. The recorder
cannot trigger itself. Stop suppresses the current microphone session until it
ends. Ignore for one hour and Never record this application are also available.
Audio before detection and device startup cannot be recovered.

Both microphone and process callbacks copy native PCM into the same buffered
writer. They never write files, allocate audio buffers, or wait for a file lock.
Each source has preallocated 4096-frame blocks covering roughly 30 seconds
(11 MiB for stereo Float32 at 48 kHz). The bound prevents an indefinitely stalled
disk from consuming all memory; preallocation keeps allocation off the audio
callback. Short callbacks pack into blocks, while timestamp discontinuities can
reduce the buffered duration. When full, the writer drops incoming frames,
continues recording, preserves their timing as silence, and records a warning.
Larger memory capacity cannot fix a permanently stalled or full disk.

A writer queue saves timestamp-aligned PCM CAF files: gaps become silence and
overlapping samples are trimmed. All native inputs stop before any file writer drains; archive duration ends at
the Stop request. File handles close after copied samples drain. The microphone uses native device formats and restarts in
a new segment on device changes, stopped delivery, or an engine failure.
Process output is discovered every 500 ms. Quiet processes keep their taps;
exited processes release HAL resources while retaining their saved audio.
Each new process lifetime or restarted tap receives a new track UUID.

A failed source is stopped and retried after ten seconds while healthy sources
continue. Device changes retry the microphone immediately. Lost audio and retry
intervals are recorded in the journal and manifest, and the menu shows current
audio health. Missing microphone callbacks for five seconds trigger a restart;
quiet process audio does not. Storage or journal failures stop the session and
retry after ten seconds.
Manual recordings start a new session automatically; automatically detected
meetings return to monitoring and restart while microphone activity continues.
Explicit Stop ends manual retry intent and suppresses automatic restart.
The error menu also offers Stop retrying.

## Archive publication and recovery

Capture performs no live transcoding. After Stop, the app resumes monitoring
while a worker aligns the native segments into one archive: mono microphone
Opus at 96 kb/s VBR and stereo Opus at 128 kb/s VBR for each process. Tracks use
48 kHz, 20 ms frames, the audio application, complexity 10, and no DTX. All tracks
extend with silence to the session end.

Finalization verifies stream metadata and decodes all streams in one pass,
publishes and synchronizes the archive, then writes a synchronized manifest with
its SHA-256. Only after publication succeeds does it remove raw inputs and the
journal. A source interruption or audio drop produces an incomplete manifest.
Unreadable inputs fail processing and are retained for recovery.

The manifest and matching archive are the commit record. Recovery verifies a
published archive and finishes cleanup without rebuilding it from partially
deleted inputs. A checksum mismatch preserves all remaining files. Uncommitted
sessions recover using journaled offsets and audio-derived segment lengths.
Open journals omit an end time; stopped journals retain the requested cutoff
through recovery, even when raw audio extends past it.
Header-only files are marked unrecoverable; files without a journal remain
available for manual salvage. The menu offers Recover and Show files for each
interrupted recording. Recovery does not require recording permissions.

Finalization and recovery share one serial job queue. Workers cannot replace
live capture state. A job with no encoder timestamp advancement or other work
progress for 120 seconds is stopped; healthy long jobs have no total duration
limit. **Cancel background processing** cancels queued and running archive jobs.
Original audio remains available for retry. Recorder exit also closes the
worker's lifetime pipe. Each worker cancels only its own unreaped tool PID,
under a shared spawn/signal/reap lock. Per-recording file locks exclude concurrent
capture and recovery and are released by the OS on process exit.

## Permissions and optional metadata

Startup requests Microphone and System Audio Recording access, records 1.5
seconds through the real capture paths, and verifies saved microphone frames.
Silence is valid. On a quiet system the menu reports that no system audio was
observed; play audio and retry to check delivery. The worker also exercises
folder access and archive creation with a 15-second deadline. Disposable audio
and archives are deleted. Recording stays disabled until this check succeeds.

The menu shows Microphone, System Audio Recording, Documents / Files & Folders,
and optional Accessibility access. Each row opens its Settings pane. Folder and
Accessibility checks refresh when the menu opens or the app becomes active;
System Audio status reflects the last recording check. Status refresh does not
record audio. Permission rechecks cannot interrupt recording. Use Copy error
details on a failed check, then Retry after addressing it. macOS may require
quitting and reopening the installed app after a permission change.

An optional worker reads only the meeting application's focused-window title
every five seconds, with a 200 ms timeout per Accessibility message. It never
traverses the application tree or subscribes to interface notifications. Its
append-only, checksummed `*-accessibility.jsonl` sidecar records title changes
and probe start/stop events. Metadata failures never interrupt audio. Normal Stop
allows two seconds to close the log; a closed lifetime pipe also stops a hung
probe. Publication waits at most three seconds for its file lock. Window titles
can contain private information and remain local.

## Diagnostics

Audio diagnostics use the macOS unified log, subsystem
`ventures.wicked.MeetingCapture`, category `audio`. In Console, filter for that
subsystem. To export the last hour:

```sh
/usr/bin/log show --last 1h --style compact \
  --predicate 'subsystem == "ventures.wicked.MeetingCapture" AND category == "audio"' \
  > ~/Desktop/MeetingCapture-audio.log
```

Logs include checks, capture/retry transitions, process identities, selected and
default device IDs/UIDs, hardware rates and buffer sizes, native formats,
configuration changes, tap/aggregate creation and teardown, and OSStatus errors.
Five-second health snapshots report engine state, callback/frame counts, last
callback age, queue occupancy and peak, dropped and written frames, and write
duration. These measure delivery, not audibility. Audio callbacks update only
atomic counters; formatting and logging run elsewhere. Logs contain filenames,
application/device identities and errors, but no audio or interface text.
Export soon after an incident and record its time. Diagnosing a failure inside
Zoom may also require Zoom logs or an Audio System Trace.

## Startup and restart

The bundled SMAppService launch agent starts the app at login and restarts it
after exit, with a ten-second throttle. LaunchServices reuses a manually opened
instance. Registration failures and Login Items approval appear in the menu.
Disable **Start at login and keep running** to keep the app open without automatic
restart; **Disable and Quit** unregisters the agent before exiting. These choices
survive upgrades and manual launches. Enable the toggle to resume automatic
startup and restart. Logout stops recording until the next login.

## macOS acceptance

Record Zoom and a windowless FaceTime call while a browser plays audio. Verify
separate process streams, microphone sound, and alignment for late-starting
processes. Change and disconnect input devices; healthy process audio must
continue and the microphone must recover in a new segment. Check quiet/resumed
processes, process exit, manual recording while muted, explicit Stop suppression,
and repeated start/stop cycles with no aggregate devices left behind. Exercise
cancellation and interrupted recovery. Correlate a Zoom microphone failure with
the diagnostic logs; its cause cannot be established from Meeting Capture alone.

Enable keep-running, approve Login Items if requested, and verify login startup
and a single app instance after a crash. Disable it and verify the app stays
stopped after exit and after the next login. Full release acceptance still needs
at least five meetings, three hours, Zoom and two other applications, covering
false triggers, HUD behavior, archive quality and interrupted recovery.
