# Meeting Capture

A local macOS menu-bar recorder. It starts when another application uses a
microphone and records two separate tracks: microphone input and all system
output. One global audio tap captures output without application windows,
including FaceTime's call bubble. Notifications, music and other application
audio are included. Automatic recording stops five seconds after the triggering
app releases its microphone. Manual recording continues until Stop.

Recordings live in `~/Documents/Meetings/YYYY/MM/`: one audio-only Matroska
(`.mka`) archive and a version 3 JSON manifest. `speech2md` reads the microphone
and system tracks independently.

## Build and install

Requires macOS 15 or later on Apple Silicon and Xcode. Build through Nix:

```sh
nix build .#meeting-capture
```

The Mac's Nix configuration installs and developer-signs the bundle. After a
merged update, refresh its `all2md` input and activate the Mac configuration.
Finish any recording, then choose **Disable and Quit** before switching builds.
Re-enable **Start at login and keep running** after launching the new build.

For a standalone local update, run from the repository root after quitting:

```sh
bash meeting-capture/install-local.sh
```

This builds with Nix, runs the two local tests, signs the staged app and worker
with the installed app's identity, and atomically replaces the app. It requires
the existing developer-signed app and its certificate/private key in Keychain.
The Nix package itself is ad-hoc signed; permission testing uses the installed,
developer-signed copy. The installer retains the `result` link for Nix runtime
dependencies.

`project.yml` and the checked-in Xcode project describe the same targets. To run
the two archive/recovery tests locally on a Mac:

```sh
nix develop .#meeting-capture --command bash meeting-capture/test-local.sh
```

There is no CI workflow. Test live microphone/device changes, Zoom and windowless
FaceTime calls, separate tracks, recovery, and login/crash restart on the Mac.

## Recording and recovery

One serial queue owns devices and the timing journal. A shared buffered writer
keeps disk I/O out of audio callbacks. Each source has about thirty seconds of
preallocated storage (11 MiB for stereo Float32 at 48 kHz). If the writer falls
behind, it drops incoming frames, marks the recording incomplete, and continues.
Missing intervals become silence; queued valid audio is retained.

An unavailable source retries every ten seconds while other sources continue.
A stopped microphone engine or five seconds without microphone input starts a
new segment. Device changes use a fresh engine. A journal failure stops capture
and displays an error; fix the problem and choose Retry. Explicit Stop
suppresses automatic capture until the triggering microphone client goes away.

Stop halts all inputs before draining their writers. A background worker aligns
segments and creates an archive with mono microphone Opus at 96 kb/s and stereo
system Opus at 128 kb/s. Restarts join into the same two tracks. Tracks extend with silence to the Stop
time. Jobs run one at a time and can be cancelled from the menu. Quitting lets
an already running archive job finish.

Raw CAF audio and the timing journal remain until the archive has been verified
and a checksummed manifest saved. Recover handles interrupted recordings and
finishes cleanup of an already published archive. Recovery mixes output from
older per-process journals into the system track. Failed recovery preserves the
remaining files; Show files exposes them for inspection. Empty files remain
untouched. Archiving failures never stop a new live recording.

## Permissions and diagnostics

Startup requests Microphone access and checks that the meetings folder is
writable. macOS requests System Audio Recording access when capturing system
audio. The Permissions menu opens the relevant System Settings panes. No startup
test recording or Accessibility permission is needed.

Unified logs use subsystem `ventures.wicked.MeetingCapture`, category `audio`.
They include device selection, native formats, source failures, queue occupancy,
dropped frames and input age. Audio callbacks only copy data and update atomics;
logging runs elsewhere. Logs contain application/device identities and filenames,
not audio. Export soon after a failure and note its time:

```sh
/usr/bin/log show --last 1h --style compact \
  --predicate 'subsystem == "ventures.wicked.MeetingCapture" AND category == "audio"' \
  > ~/Desktop/MeetingCapture-audio.log
```

**Start at login and keep running** uses one SMAppService launch agent with a
ten-second restart throttle. **Disable and Quit** unregisters it before exiting.
macOS may require Login Items approval. These choices persist across upgrades.
