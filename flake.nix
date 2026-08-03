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
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      darwinSystem = "aarch64-darwin";
      docSystems = [
        darwinSystem
        "aarch64-linux"
        "x86_64-linux"
      ];
      forDocSystems = nixpkgs.lib.genAttrs docSystems;
      pkgs = nixpkgs.legacyPackages.${darwinSystem};
      pagesProject = ./tools/pages2md;
      speechProject = ./tools/speech2md;
      docProject = ./tools/doc2md;
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
          jsonschema
        ]
      );
      pages2md = pkgs.writeShellApplication {
        name = "pages2md";
        runtimeInputs = [
          pkgs.djvulibre
          pkgs.poppler-utils
        ];
        text = ''
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
          exec ${speechEnvironment}/bin/speech2md "$@"
        '';
      };
      meeting-capture = pkgs.callPackage ./apps/meeting-capture/package.nix { };
      mkDoc2md =
        system:
        let
          systemPkgs = nixpkgs.legacyPackages.${system};
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
      mkDocApp = system: {
        type = "app";
        program = "${mkDoc2md system}/bin/doc2md";
      };
      mkDocIndependenceCheck =
        system:
        let
          systemPkgs = nixpkgs.legacyPackages.${system};
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
        system:
        let
          systemPkgs = nixpkgs.legacyPackages.${system};
        in
        systemPkgs.mkShell {
          packages = [
            systemPkgs.git
            systemPkgs.python3
            systemPkgs.uv
          ];
          shellHook = ''
            echo "Run: uv sync --project tools/doc2md --extra dev"
          '';
        };
    in
    {
      packages =
        forDocSystems (system: {
          doc2md = mkDoc2md system;
        })
        // {
          ${darwinSystem} = {
            default = pages2md;
            inherit meeting-capture pages2md speech2md;
            doc2md = mkDoc2md darwinSystem;
          };
        };

      apps =
        forDocSystems (system: {
          doc2md = mkDocApp system;
        })
        // {
          ${darwinSystem} = {
            default = {
              type = "app";
              program = "${pages2md}/bin/pages2md";
            };
            speech2md = {
              type = "app";
              program = "${speech2md}/bin/speech2md";
            };
            doc2md = mkDocApp darwinSystem;
          };
        };

      checks =
        forDocSystems (system: {
          doc2md = mkDoc2md system;
          doc2md-independence = mkDocIndependenceCheck system;
        })
        // {
          ${darwinSystem} = {
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
            packaged-clis =
              pkgs.runCommand "packaged-clis"
                {
                  nativeBuildInputs = [
                    pages2md
                    speech2md
                  ];
                }
                ''
                  export HOME="$TMPDIR/home"
                  export XDG_CACHE_HOME="$TMPDIR/cache"
                  mkdir -p "$HOME" "$XDG_CACHE_HOME"
                  pages2md --help > /dev/null
                  speech2md --help > /dev/null
                  test ! -e "$XDG_CACHE_HOME/pages2md/venv"
                  test ! -e "$XDG_CACHE_HOME/speech2md/venv"
                  touch "$out"
                '';
            meeting-capture-schema =
              pkgs.runCommand "meeting-capture-schema-check" { nativeBuildInputs = [ testPython ]; }
                ''
                  python -c 'import json, jsonschema; schema=json.load(open("${./schemas/meeting-capture-v1.schema.json}")); fixture=json.load(open("${./apps/meeting-capture/Tests/Fixtures/manifest-v1.json}")); jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(fixture)'
                  touch "$out"
                '';
            doc2md = mkDoc2md darwinSystem;
            doc2md-independence = mkDocIndependenceCheck darwinSystem;
          };
        };

      devShells =
        forDocSystems (system: {
          doc2md = mkDocShell system;
        })
        // {
          ${darwinSystem} = rec {
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

            doc2md = mkDocShell darwinSystem;
            default = pages2md;
          };
        };

      formatter = forDocSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
