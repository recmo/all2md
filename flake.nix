{
  description = "Local-native ebook and document conversion to auditable Markdown";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = nixpkgs.legacyPackages.${system};
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
          exec uv run --frozen --extra ocr --project ${self} ebook2md "$@"
        '';
      };
    in
    {
      packages.${system} = {
        default = ebook2md;
        inherit ebook2md;
      };

      apps.${system}.default = {
        type = "app";
        program = "${ebook2md}/bin/ebook2md";
      };

      checks.${system}.source = pkgs.runCommand "ebook2md-source-check" { nativeBuildInputs = [ pkgs.python3 ]; } ''
        export PYTHONPYCACHEPREFIX="$out/pycache"
        python -m compileall -q ${self}/src
        touch "$out/passed"
      '';

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.djvulibre
          pkgs.poppler-utils
          pkgs.python3
          pkgs.uv
        ];
        shellHook = ''
          echo "Run: uv sync --extra dev --extra ocr"
        '';
      };

      formatter.${system} = pkgs.nixfmt;
    };
}

