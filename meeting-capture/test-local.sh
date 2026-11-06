#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != Darwin ]; then
    echo 'Meeting Capture tests require macOS.' >&2
    exit 1
fi
derived="$(mktemp -d)"
trap 'rm -rf "$derived"' EXIT
xcodebuild -project meeting-capture/MeetingCapture.xcodeproj \
    -scheme MeetingCapture -configuration Debug -derivedDataPath "$derived" \
    CODE_SIGNING_ALLOWED=NO ENABLE_DEBUG_DYLIB=YES build-for-testing
app="$derived/Build/Products/Debug/MeetingCapture.app"
# Load the app's compiled code while xctest supplies the command-line test host.
# This works for launchd CI accounts without a GUI testmanagerd session.
export DYLD_LIBRARY_PATH="$app/Contents/MacOS${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
xcrun xctest "$app/Contents/PlugIns/MeetingCaptureTests.xctest"
