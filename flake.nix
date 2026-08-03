{
  description = "Local-first tools that turn source material into auditable Markdown";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = nixpkgs.legacyPackages.${system};
      pagesProject = ./tools/pages2md;
      speechProject = ./tools/speech2md;
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
      pages2md = pkgs.writeShellApplication {
        name = "pages2md";
        runtimeInputs = [
          pkgs.djvulibre
          pkgs.poppler-utils
          pkgs.uv
        ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/pages2md"
          mkdir -p "$cache_root"
          export UV_PROJECT_ENVIRONMENT="$cache_root/venv"
          exec uv run --frozen --extra ocr --project ${pagesProject} pages2md "$@"
        '';
      };
      speech2md = pkgs.writeShellApplication {
        name = "speech2md";
        runtimeInputs = [
          pkgs.ffmpeg
          pkgs.uv
        ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/speech2md"
          mkdir -p "$cache_root/torch"
          export TORCH_HOME="$cache_root/torch"
          export UV_PROJECT_ENVIRONMENT="$cache_root/venv"
          exec uv run --frozen --project ${speechProject} speech2md "$@"
        '';
      };
      meeting-capture = pkgs.callPackage ./apps/meeting-capture/package.nix { };
    in
    {
      packages.${system} = {
        default = pages2md;
        inherit meeting-capture pages2md speech2md;
      };

      apps.${system} = {
        default = {
          type = "app";
          program = "${pages2md}/bin/pages2md";
        };
        speech2md = {
          type = "app";
          program = "${speech2md}/bin/speech2md";
        };
      };

      checks.${system} = {
        pages2md-source =
          pkgs.runCommand "pages2md-source-check" { nativeBuildInputs = [ pkgs.python3 ]; }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${pagesProject}/src
              touch "$out/passed"
            '';
        pages2md-tests = pkgs.runCommand "pages2md-tests" { nativeBuildInputs = [ testPython ]; } ''
          export PYTHONPATH=${pagesProject}/src
          export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
          pytest -q ${pagesProject}/tests
          touch "$out"
        '';
        speech2md-source =
          pkgs.runCommand "speech2md-source-check" { nativeBuildInputs = [ pkgs.python3 ]; }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${speechProject}/src
              touch "$out/passed"
            '';
        speech2md-tests =
          pkgs.runCommand "speech2md-tests"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
            }
            ''
              export PYTHONPATH=${speechProject}/src
              export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
              pytest -q ${speechProject}/tests
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
        pages2md = pkgs.mkShell {
          packages = [
            pkgs.djvulibre
            pkgs.poppler-utils
            pkgs.python3
            pkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project tools/pages2md --extra dev --extra ocr"
          '';
        };

        meeting-capture = pkgs.mkShell {
          packages = [ pkgs.xcodegen ];
          shellHook = ''
            echo "Run: cd apps/meeting-capture && xcodegen generate"
          '';
        };

        speech2md = pkgs.mkShell {
          packages = [
            pkgs.ffmpeg
            pkgs.python3
            pkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project tools/speech2md --extra dev"
          '';
        };

        default = pages2md;
      };

      formatter.${system} = pkgs.nixfmt;
    };
}
