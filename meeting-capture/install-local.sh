#!/bin/bash
# Rebuild, test, and replace the installed app under its existing signing identity.
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then
    echo 'Meeting Capture must be built and installed on macOS.' >&2
    exit 1
fi
cd "$(dirname "$0")/.."
installed="$HOME/Applications/MeetingCapture.app"
test -d "$installed"
test ! -L "$installed"
if /usr/bin/pgrep -x MeetingCapture >/dev/null; then
    echo 'Quit Meeting Capture first, then rerun this command.' >&2
    exit 1
fi
staging="$(mktemp -d "$HOME/Applications/.meeting-capture-install.XXXXXX")"
trap 'rm -rf "$staging"' EXIT

nix build .#meeting-capture
nix develop .#meeting-capture --command bash meeting-capture/test-local.sh
bash meeting-capture/sign-local.sh \
    result/Applications/MeetingCapture.app "$staging/MeetingCapture.app" "$installed"

if /usr/bin/pgrep -x MeetingCapture >/dev/null; then
    echo 'Meeting Capture is running. Quit it before installing.' >&2
    exit 1
fi
/usr/bin/chflags -R nouchg "$installed"
rm -rf "$installed"
mv "$staging/MeetingCapture.app" "$installed"
/usr/bin/codesign --verify --deep --strict "$installed"
/usr/bin/chflags -R uchg "$installed"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$installed"
/usr/bin/open "$installed"
