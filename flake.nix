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
      docSystems = [
        system
        "aarch64-linux"
        "x86_64-linux"
      ];
      forDocSystems = nixpkgs.lib.genAttrs docSystems;
      pkgs = nixpkgs.legacyPackages.${system};
      pagesProject = ./pages2md;
      speechProject = ./speech2md;
      reviewProject = ./speech-review;
      docProject = ./doc2md;
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
      reviewEnvironment = mkPythonEnvironment {
        name = "speech-review";
        project = reviewProject;
      };
      pagesTestEnvironment = mkPythonEnvironment {
        name = "pages2md";
        project = pagesProject;
        extras = [
          "ocr"
          "dev"
        ];
      };
      pages2md = pkgs.writeShellApplication {
        name = "pages2md";
        runtimeInputs = [
          pkgs.djvulibre
          pkgs.poppler-utils
          pkgs.nodejs
        ];
        text = ''
          export PAGES2MD_VERSION=${pagesVersion}
          export PAGES2MD_KATEX_MODULE=${pkgs.katex}/lib/node_modules/katex
          exec ${pagesEnvironment}/bin/pages2md "$@"
        '';
      };
      speech2md = pkgs.writeShellApplication {
        name = "speech2md";
        runtimeInputs = [ pkgs.ffmpeg ];
        text = ''
          cache_root="''${XDG_CACHE_HOME:-$HOME/.cache}/speech2md"
          mkdir -p "$cache_root/torch"
          export TORCH_HOME="$cache_root/torch"
          export SPEECH2MD_VERSION=${pagesVersion}
          exec ${speechEnvironment}/bin/speech2md "$@"
        '';
      };
      speech-review = pkgs.writeShellApplication {
        name = "speech-review";
        runtimeInputs = [ speech2md ];
        text = ''
          exec ${reviewEnvironment}/bin/speech-review "$@"
        '';
      };
      meeting-capture = pkgs.callPackage ./meeting-capture/package.nix { };
      mkDoc2md =
        docSystem:
        let
          systemPkgs = nixpkgs.legacyPackages.${docSystem};
          pythonPackages = systemPkgs.python3Packages;
        in
        pythonPackages.buildPythonApplication {
          pname = "doc2md";
          version = "0.1.0";
          src = docProject;
          pyproject = true;

          build-system = [ pythonPackages.hatchling ];
          dependencies = [ pythonPackages.requests ];
          nativeBuildInputs = [ systemPkgs.makeWrapper ];
          nativeCheckInputs = [
            systemPkgs.git
            pythonPackages.pytest
          ];

          checkPhase = ''
            runHook preCheck
            pytest -q
            runHook postCheck
          '';

          postFixup = ''
            wrapProgram "$out/bin/doc2md" --prefix PATH : ${nixpkgs.lib.makeBinPath [ systemPkgs.git ]}
          '';

          pythonImportsCheck = [ "doc2md.cli" ];

          meta = {
            description = "Extract durable Markdown from Google Docs and Notion";
            mainProgram = "doc2md";
            license = nixpkgs.lib.licenses.mit;
          };
        };
      mkDocApp = docSystem: {
        type = "app";
        program = "${mkDoc2md docSystem}/bin/doc2md";
      };
      mkDocIndependenceCheck =
        docSystem:
        let
          systemPkgs = nixpkgs.legacyPackages.${docSystem};
        in
        systemPkgs.runCommand "doc2md-independence-check"
          {
            nativeBuildInputs = [ (systemPkgs.python3.withPackages (ps: [ ps.pytest ])) ];
          }
          ''
            export PYTHONPATH=${docProject}/src
            export DOC2MD_REPOSITORY_ROOT=${./.}
            pytest -q -p no:cacheprovider ${docProject}/tests/test_independence.py
            touch "$out"
          '';
      mkDocShell =
        docSystem:
        let
          systemPkgs = nixpkgs.legacyPackages.${docSystem};
        in
        systemPkgs.mkShell {
          packages = [
            systemPkgs.git
            systemPkgs.python3
            systemPkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project doc2md --extra dev"
          '';
        };
    in
    {
      packages =
        forDocSystems (docSystem: {
          doc2md = mkDoc2md docSystem;
        })
        // {
          ${system} = {
            default = pages2md;
            inherit
              meeting-capture
              pages2md
              speech-review
              speech2md
              ;
            doc2md = mkDoc2md system;
          };
        };

      apps =
        forDocSystems (docSystem: {
          doc2md = mkDocApp docSystem;
        })
        // {
          ${system} = {
            default = {
              type = "app";
              program = "${pages2md}/bin/pages2md";
            };
            speech2md = {
              type = "app";
              program = "${speech2md}/bin/speech2md";
            };
            speech-review = {
              type = "app";
              program = "${speech-review}/bin/speech-review";
            };
            doc2md = mkDocApp system;
          };
        };

      checks =
        forDocSystems (docSystem: {
          doc2md = mkDoc2md docSystem;
          doc2md-independence = mkDocIndependenceCheck docSystem;
        })
        // {
          ${system} = {
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
                  nativeBuildInputs = [
                    pagesTestEnvironment
                    pkgs.nodejs
                  ];
                  PAGES2MD_VERSION = pagesVersion;
                  PAGES2MD_KATEX_MODULE = "${pkgs.katex}/lib/node_modules/katex";
                }
                ''
                  export PYTHONPATH=${pagesProject}/src
                  export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
                  pytest -q -o cache_dir="$TMPDIR/pytest-cache" ${pagesProject}/tests
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
            speech-review-source =
              pkgs.runCommand "speech-review-source-check"
                {
                  nativeBuildInputs = [ pkgs.python3 ];
                }
                ''
                  export PYTHONPYCACHEPREFIX="$out/pycache"
                  python -m compileall -q ${reviewProject}/src
                  touch "$out/passed"
                '';
            speech-review-tests =
              pkgs.runCommand "speech-review-tests"
                {
                  nativeBuildInputs = [
                    (pkgs.python3.withPackages (ps: [
                      ps.numpy
                      ps.pytest
                      ps.pyyaml
                    ]))
                  ];
                }
                ''
                  export PYTHONPATH=${reviewProject}/src
                  export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
                  pytest -q ${reviewProject}/tests
                  touch "$out"
                '';
            packaged-clis =
              pkgs.runCommand "packaged-clis"
                {
                  nativeBuildInputs = [
                    pages2md
                    pkgs.darwin.cctools
                    speech2md
                    speech-review
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
                  speech-review --help > /dev/null
                  ${reviewEnvironment}/bin/python -c 'from speech_review.server import STATIC; assert (STATIC / "index.html").is_file()'
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
            doc2md = mkDoc2md system;
            doc2md-independence = mkDocIndependenceCheck system;
          };
        };

      devShells =
        forDocSystems (docSystem: {
          doc2md = mkDocShell docSystem;
        })
        // {
          ${system} = rec {
            pages2md = pkgs.mkShell {
              PAGES2MD_KATEX_MODULE = "${pkgs.katex}/lib/node_modules/katex";
              packages = [
                pkgs.djvulibre
                pkgs.poppler-utils
                pkgs.python3
                pkgs.uv
                pkgs.nodejs
              ];
              shellHook = ''
                echo "Run: uv sync --project pages2md --extra dev --extra ocr"
              '';
            };

            meeting-capture = pkgs.mkShell {
              packages = [
                pkgs.ffmpeg
                pkgs.xcodegen
              ];
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

            speech-review = pkgs.mkShell {
              packages = [
                pkgs.python3
                pkgs.uv
              ];
              shellHook = ''
                echo "Run: uv sync --project speech-review --extra dev"
              '';
            };

            doc2md = mkDocShell system;
            default = pages2md;
          };
        };

      formatter = forDocSystems (docSystem: nixpkgs.legacyPackages.${docSystem}.nixfmt);
    };
}
