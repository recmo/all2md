{
  description = "Local-first tools that turn source material into auditable Markdown";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      system = "aarch64-darwin";
      pkgs = nixpkgs.legacyPackages.${system};
      pagesProject = ./pages2md;
      speechProject = ./speech2md;
      pagesVersion =
        if self ? rev then
          self.rev
        else if self ? dirtyRev then
          builtins.substring 0 40 self.dirtyRev
        else
          "unknown";
      mkPythonEnvironment =
        {
          name,
          project,
          extras ? [ ],
        }:
        let
          workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = project; };
          overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
          buildSystemOverrides = final: prev: {
            mlx = prev.mlx.overrideAttrs (old: {
              nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.darwin.cctools ];
              postFixup = (old.postFixup or "") + ''
                for extension in "$out"/lib/python*/site-packages/mlx/core*.so; do
                  install_name_tool \
                    -rpath @loader_path/lib \
                    ${final."mlx-metal"}/lib/python${pkgs.python313.pythonVersion}/site-packages/mlx/lib \
                    "$extension"
                done
              '';
            });
            mlx-vlm = prev.mlx-vlm.overrideAttrs (old: {
              nativeBuildInputs =
                (old.nativeBuildInputs or [ ])
                ++ final.resolveBuildSystem {
                  setuptools = [ ];
                };
            });
          };
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope
              (
                nixpkgs.lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  overlay
                  buildSystemOverrides
                ]
              );
        in
        pythonSet.mkVirtualEnv "${name}-env" { ${name} = extras; };
      pagesEnvironment = mkPythonEnvironment {
        name = "pages2md";
        project = pagesProject;
        extras = [ "ocr" ];
      };
      speechEnvironment = mkPythonEnvironment {
        name = "speech2md";
        project = speechProject;
      };
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
          tqdm
        ]
      );
      pages2md = pkgs.writeShellApplication {
        name = "pages2md";
        runtimeInputs = [
          pkgs.djvulibre
          pkgs.poppler-utils
        ];
        text = ''
          export PAGES2MD_VERSION=${pagesVersion}
          exec ${pagesEnvironment}/bin/pages2md "$@"
        '';
      };
      speech2md = pkgs.writeShellApplication {
        name = "speech2md";
        runtimeInputs = [
          pkgs.ffmpeg
        ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/speech2md"
          mkdir -p "$cache_root/torch"
          export TORCH_HOME="$cache_root/torch"
          export SPEECH2MD_VERSION=${pagesVersion}
          exec ${speechEnvironment}/bin/speech2md "$@"
        '';
      };
      meeting-capture = pkgs.callPackage ./meeting-capture/package.nix { };
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
          pkgs.runCommand "pages2md-source-check"
            {
              nativeBuildInputs = [ pkgs.python3 ];
              PAGES2MD_VERSION = pagesVersion;
            }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${pagesProject}/src
              touch "$out/passed"
            '';
        pages2md-tests =
          pkgs.runCommand "pages2md-tests"
            {
              nativeBuildInputs = [ testPython ];
              PAGES2MD_VERSION = pagesVersion;
            }
            ''
              export PYTHONPATH=${pagesProject}/src
              export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
              pytest -q ${pagesProject}/tests
              touch "$out"
            '';
        speech2md-source =
          pkgs.runCommand "speech2md-source-check"
            {
              nativeBuildInputs = [ pkgs.python3 ];
              SPEECH2MD_VERSION = pagesVersion;
            }
            ''
              export PYTHONPYCACHEPREFIX="$out/pycache"
              python -m compileall -q ${speechProject}/src
              touch "$out/passed"
            '';
        speech2md-tests =
          pkgs.runCommand "speech2md-tests"
            {
              nativeBuildInputs = [
                (pkgs.python3.withPackages (ps: [
                  ps.numpy
                  ps.pytest
                  ps.pyyaml
                  ps.tqdm
                ]))
              ];
              SPEECH2MD_VERSION = pagesVersion;
            }
            ''
              export PYTHONPATH=${speechProject}/src
              export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
              pytest -q ${speechProject}/tests
              touch "$out"
            '';
        packaged-clis =
          pkgs.runCommand "packaged-clis"
            {
              nativeBuildInputs = [
                pages2md
                pkgs.darwin.cctools
                speech2md
              ];
            }
            ''
              export HOME="$TMPDIR/home"
              export XDG_CACHE_HOME="$TMPDIR/cache"
              mkdir -p "$HOME" "$XDG_CACHE_HOME"
              pages2md --help > /dev/null
              pages2md --version | grep -Eq '^pages2md [0-9a-f]{40,64}$'
              ${pagesEnvironment}/bin/python -c 'from mlx_vlm import load'
              speech2md --help > /dev/null
              speech2md --version | grep -Eq '^speech2md [0-9a-f]{40,64}$'
              for extension in \
                ${pagesEnvironment}/lib/python*/site-packages/mlx/core*.so \
                ${speechEnvironment}/lib/python*/site-packages/mlx/core*.so; do
                otool -l "$extension" \
                  | grep -Eq 'path /nix/store/[a-z0-9]+-mlx-metal-[^/]+/lib/python[^/]+/site-packages/mlx/lib '
              done
              test ! -e "$XDG_CACHE_HOME/pages2md/venv"
              test ! -e "$XDG_CACHE_HOME/speech2md/venv"
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
            echo "Run: uv sync --project pages2md --extra dev --extra ocr"
          '';
        };

        meeting-capture = pkgs.mkShell {
          packages = [ pkgs.xcodegen ];
          shellHook = ''
            echo "Run: cd meeting-capture && xcodegen generate"
          '';
        };

        speech2md = pkgs.mkShell {
          packages = [
            pkgs.ffmpeg
            pkgs.python3
            pkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project speech2md --extra dev"
          '';
        };

        default = pages2md;
      };

      formatter.${system} = pkgs.nixfmt;
    };
}
