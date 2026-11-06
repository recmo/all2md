#!/bin/bash
# Match home/remco/darwin.nix in recmo/nix, including the worker's identity.
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "usage: $0 SOURCE_APP DESTINATION_APP [INSTALLED_APP]" >&2
    exit 1
fi
source_app="$1"
destination_app="$2"
installed_app="${3:-$HOME/Applications/MeetingCapture.app}"
identity_name='Apple Development: REMCO BLOEMEN (V458N7266F)'
identifier='ventures.wicked.MeetingCapture'

test -d "$source_app"
test -d "$installed_app"
if [ -e "$destination_app" ]; then
    echo "Destination already exists: $destination_app" >&2
    exit 1
fi
/usr/bin/codesign --verify --deep --strict "$installed_app"
requirement="$(/usr/bin/codesign -d -r- "$installed_app" 2>&1 | /usr/bin/sed -n 's/^designated => //p')"
test -n "$requirement"
identity="$(/usr/bin/security find-identity -v -p codesigning \
    | /usr/bin/awk -v name="$identity_name" 'index($0, "\"" name "\"") { print $2; exit }')"
if [ -z "$identity" ]; then
    echo "Missing signing identity in the keychain: $identity_name" >&2
    exit 1
fi

# Never modify the installed app or the Nix store. Publish only after verification.
mkdir -p "$(dirname "$destination_app")"
staging="$(mktemp -d "$(dirname "$destination_app")/.meeting-capture-sign.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
app="$staging/MeetingCapture.app"
/usr/bin/ditto "$source_app" "$app"
/bin/chmod -R u+w "$app"
for target in "$app/Contents/Helpers/MeetingCaptureWorker" "$app"; do
    /usr/bin/codesign --force --sign "$identity" --timestamp=none \
        --identifier "$identifier" "-r=designated => $requirement" \
        --preserve-metadata=entitlements "$target"
    /usr/bin/codesign --verify --strict "-R=$requirement" "$target"
done
/usr/bin/codesign --verify --deep --strict "$app"
mv "$app" "$destination_app"
echo "Signed with the installed app's designated requirement: $destination_app"
