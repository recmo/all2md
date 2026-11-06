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
    CODE_SIGNING_ALLOWED=NO ENABLE_DEBUG_DYLIB=YES \
    'LD_RUNPATH_SEARCH_PATHS=$(inherited) @loader_path/../../../../MacOS' \
    build-for-testing
app="$derived/Build/Products/Debug/MeetingCapture.app"
# The relative runpath above lets the bundle load the app's compiled code.
# xctest supplies a test host without a GUI testmanagerd session.
xcrun xctest "$app/Contents/PlugIns/MeetingCaptureTests.xctest"
