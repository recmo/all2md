{
  description = "Local-first tools that turn source material into auditable Markdown";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = nixpkgs.legacyPackages.${system};
      ebookProject = ./tools/ebook2md;
      audioProject = ./tools/audio2md;
      testPython = pkgs.python3.withPackages (
        ps: with ps; [
          beautifulsoup4
          lxml
          mdformat
          mdformat-gfm
          mlx
          pillow
          pymupdf
          pytest
          jsonschema
        ]
      );
      ebook2md = pkgs.writeShellApplication {
        name = "ebook2md";
        runtimeInputs = [
          pkgs.djvulibre
          pkgs.poppler-utils
          pkgs.uv
        ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/ebook2md"
          mkdir -p "$cache_root"
          export UV_PROJECT_ENVIRONMENT="$cache_root/venv"
          exec uv run --frozen --extra ocr --project ${ebookProject} ebook2md "$@"
        '';
      };
      audio2md = pkgs.writeShellApplication {
        name = "audio2md";
        runtimeInputs = [
          pkgs.ffmpeg
          pkgs.uv
        ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/audio2md"
          mkdir -p "$cache_root/torch"
          export TORCH_HOME="$cache_root/torch"
          export UV_PROJECT_ENVIRONMENT="$cache_root/venv"
          exec uv run --frozen --project ${audioProject} audio2md "$@"
        '';
      };
      meeting-capture = pkgs.callPackage ./apps/meeting-capture/package.nix { };
    in
    {
      packages.${system} = {
        default = ebook2md;
        inherit audio2md ebook2md meeting-capture;
      };

      apps.${system} = {
        default = {
          type = "app";
          program = "${ebook2md}/bin/ebook2md";
        };
        audio2md = {
          type = "app";
          program = "${audio2md}/bin/audio2md";
        };
      };

      checks.${system} = {
        ebook2md-source =
          pkgs.runCommand "ebook2md-source-check" { nativeBuildInputs = [ pkgs.python3 ]; }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${ebookProject}/src
              touch "$out/passed"
            '';
        ebook2md-tests = pkgs.runCommand "ebook2md-tests" { nativeBuildInputs = [ testPython ]; } ''
          export PYTHONPATH=${ebookProject}/src
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          pytest -q ${ebookProject}/tests
          touch "$out"
        '';
        audio2md-source =
          pkgs.runCommand "audio2md-source-check" { nativeBuildInputs = [ pkgs.python3 ]; }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${audioProject}/src
              touch "$out/passed"
            '';
        audio2md-tests =
          pkgs.runCommand "audio2md-tests"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
            }
            ''
              export PYTHONPATH=${audioProject}/src
              export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
              pytest -q ${audioProject}/tests
              touch "$out"
            '';
        meeting-capture-schema =
          pkgs.runCommand "meeting-capture-schema-check" { nativeBuildInputs = [ testPython ]; }
            ''
              python -c 'import json, jsonschema; schema=json.load(open("${./schemas/meeting-capture-v1.schema.json}")); fixture=json.load(open("${./apps/meeting-capture/Tests/Fixtures/manifest-v1.json}")); jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(fixture)'
              touch "$out"
            '';
      };

      devShells.${system} = rec {
        ebook2md = pkgs.mkShell {
          packages = [
            pkgs.djvulibre
            pkgs.poppler-utils
            pkgs.python3
            pkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project tools/ebook2md --extra dev --extra ocr"
          '';
        };

        meeting-capture = pkgs.mkShell {
          packages = [ pkgs.xcodegen ];
          shellHook = ''
            echo "Run: cd apps/meeting-capture && xcodegen generate"
          '';
        };

        audio2md = pkgs.mkShell {
          packages = [
            pkgs.ffmpeg
            pkgs.python3
            pkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project tools/audio2md --extra dev"
          '';
        };

        default = ebook2md;
      };

      formatter.${system} = pkgs.nixfmt;
    };
}
