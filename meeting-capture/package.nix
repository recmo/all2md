{
  lib,
  stdenvNoCC,
  xcodeenv,
  xcodegen,
}:

let
  xcodeWrapper = xcodeenv.composeXcodeWrapper { };
in
stdenvNoCC.mkDerivation {
  pname = "meeting-capture";
  version = "0.1.0";
  src = ./.;

  nativeBuildInputs = [
    xcodegen
    xcodeWrapper
  ];

  buildPhase = ''
    runHook preBuild

    export HOME="$TMPDIR/home"
    export USER=nix-builder
    mkdir -p "$HOME"
    xcodegen generate
    xcodebuild \
      -project MeetingCapture.xcodeproj \
      -scheme MeetingCapture \
      -configuration Release \
      -derivedDataPath "$TMPDIR/DerivedData" \
      CODE_SIGNING_ALLOWED=NO \
      build

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/Applications"
    cp -R "$TMPDIR/DerivedData/Build/Products/Release/MeetingCapture.app" "$out/Applications/"
    codesign \
      --force \
      --sign - \
      --entitlements MeetingCapture.entitlements \
      "$out/Applications/MeetingCapture.app"

    runHook postInstall
  '';

  meta = {
    description = "Local-only macOS meeting recorder";
    homepage = "https://github.com/recmo/all2md";
    license = lib.licenses.mit;
    platforms = [ "aarch64-darwin" ];
    mainProgram = "MeetingCapture";
  };
}
