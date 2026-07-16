{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    systems = {
      url = "github:nix-systems/default";
    };
    flake-utils = {
      url = "github:numtide/flake-utils";
      inputs.systems.follows = "systems";
    };
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      systems,
      git-hooks,
    }:
    let
      for-systems = flake-utils.lib.eachDefaultSystem (
        system:
        let
          cargo-ua-overlay =
            final: prev:
            let
              fetchurlWithUA =
                args:
                prev.fetchurl (
                  args
                  // {
                    curlOptsList = (args.curlOptsList or [ ]) ++ [
                      "--user-agent"
                      "genvm (kira@genlayerlabs.com)"
                    ];
                  }
                );
              importCargoLockWithUA = prev.rustPlatform.importCargoLock.override {
                fetchurl = fetchurlWithUA;
              };
            in
            {
              rustPlatform = prev.rustPlatform // {
                importCargoLock = importCargoLockWithUA;
                buildRustPackage = prev.rustPlatform.buildRustPackage.override {
                  importCargoLock = importCargoLockWithUA;
                };
              };
            };
          pkgs-overlays = [ cargo-ua-overlay ];
          pkgs = import nixpkgs {
            inherit system;
            overlays = pkgs-overlays;
          };

          genvm-tool = import ./support/tools/genvm-tool { inherit pkgs; };

          deps = import ./libs/deps { inherit pkgs; };

          custom-rust = import ./support/rust.nix {
            inherit pkgs deps system;
            withLinters = true;
            withZig = false;
            withWasi = true;
          };
          custom-rust-builder = import ./support/compile-rust.nix {
            inherit pkgs system deps;
            zig = import ./support/zig.nix { inherit pkgs deps system; };
          };

          custom-cargo-afl = custom-rust-builder rec {
            name = "cargo-afl";
            version = "0.17.1";
            src = deps."cargo-afl-0.17.1";

            target = system;

            cargoLock.lockFile = "${src}/Cargo.lock";

            nativeBuildInputs = [
              pkgs.gnumake
              pkgs.makeWrapper
            ];

            postBuild = ''
              XDG_DATA_HOME="$out/data" ./target/*/release/cargo-afl afl config --build --verbose
            '';

            installPhase = ''
              mkdir -p $out/bin
              cp target/__out $out/bin/cargo-afl
              wrapProgram $out/bin/cargo-afl \
              --set XDG_DATA_HOME "$out/data"
            '';
          };

          packages-0 = with pkgs; [
            bash
            xz
            zlib
            git
            python312
            coreutils
            which
            jq
            stdenv.cc
            glibc
            nix
            genvm-tool
          ];
          packages-rust = [ custom-rust ];
          packages-debug-test = with pkgs; [
            (pkgs.ninja.overrideAttrs (old: {
              postPatch = old.postPatch + ''
                substituteInPlace src/subprocess-posix.cc \
                --replace '"/bin/sh"' '"${pkgs.bash}/bin/bash"'
              '';
            }))
            ruby
            gcc

            custom-cargo-afl
            llvmPackages.libllvm

            python312Packages.jsonnet
            pkgs.python312Packages.aiohttp
            wabt
          ];
          # Docs toolchain from nix instead of a poetry venv: sphinx + the
          # extensions conf.py loads. mermaid-cli is the `mmdc` the
          # sphinxcontrib.mermaid svg output shells out to. The Python SDK
          # autodoc (and its numpy dep) and the text→txt merge (formerly ruby,
          # now docs/website/merge_txts.py) moved to the executor sub-sites.
          docs-python = pkgs.python312.withPackages (
            ps: with ps; [
              sphinx
              myst-parser
              pydata-sphinx-theme
              sphinxcontrib-mermaid
              sphinxcontrib-openapi
            ]
          );
          packages-gen-docs = with pkgs; [
            docs-python
            mermaid-cli
          ];
          packages-py-test = with pkgs; [
            # aflplusplus # currently we don't run fuzzing on CI
            python312
            poetry
            # pytest + plugins so `poetry run -- pytest` has the
            # coverage plugin available (addopts uses --cov)
            python312Packages.pytest
            python312Packages.pytest-cov
            python312Packages.pytest-xdist
          ];
          shell-hook-base = ''
            export PATH="$(pwd)/support/tools/git-third-party:$PATH"
            export CARGO_LD_LIBRARY_PATH="${toString pkgs.xz.out}/lib:${toString pkgs.zlib.out}/lib:${pkgs.stdenv.cc.cc.lib}/lib:${toString pkgs.glibc}/lib"
            export LLVM_PROFILE_FILE=/dev/null
            export LSQLITE3_SRC="${deps."lsqlite3-0.9.6"}"
            export LUA_INCLUDE="${manager-release-args.lua-src}"
            export CARGO_TARGET_DIR="$(pwd)/build/ya-build/rust-target"
          '';

          # ---- Commit hooks (git-hooks.nix) ------------------------
          # Formatters/linters + local guards for the MANAGER tree only.
          # Executor submodules ship their own top-level flake + hooks
          # (each pinned to its own toolchain), so `^executors/` is dropped
          # here and every executor's own `nix flake check` covers its
          # files. Wired into CI by support/ci/pipelines/commit-hooks.sh and
          # installed into .git/hooks by the `full` dev shell's shellHook.
          hooks-python = pkgs.python312;
          # cargo fmt per crate: each Cargo.toml picks up its own edition, so
          # the manager's independent workspaces all format correctly.
          cargo-fmt-all = pkgs.writeShellScript "cargo-fmt-all" ''
            set -euo pipefail
            export PATH="${pkgs.cargo}/bin:${pkgs.rustfmt}/bin:$PATH"
            rc=0
            # git ls-files never descends into the executor submodule gitlinks,
            # but exclude executors/ explicitly too: their Rust is formatted by
            # each executor's own flake, against its own pinned toolchain.
            while IFS= read -r toml; do
              ( cd "$(dirname -- "$toml")" && cargo fmt ) || rc=1
            done < <(git ls-files '*Cargo.toml' ':(exclude)executors/')
            exit $rc
          '';
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            excludes = [
              "^executors/"
              "^build/"
              "^\\.direnv/"
            ];
            hooks = {
              # --- generic checks ---------------------------------------
              trim-trailing-whitespace = {
                enable = true;
                excludes = [
                  "^\\.git-third-party"
                  "/fuzz/"
                ];
              };
              end-of-file-fixer = {
                enable = true;
                excludes = [
                  "^\\.git-third-party"
                  "/fuzz/"
                ];
              };
              check-added-large-files.enable = true;
              check-json = {
                enable = true;
                excludes = [
                  "^\\.git-third-party"
                  "/tsconfig\\.json$"
                ];
              };
              check-yaml.enable = true;
              check-toml.enable = true;
              check-merge-conflicts.enable = true;
              taplo.enable = true;
              editorconfig-checker = {
                enable = true;
                # text-only (the built-in defaults to every file): keep binary
                # blobs out of it, mirroring the old engine's `text` pseudo-type.
                types = [ "text" ];
                excludes = [
                  "git-third-party"
                  "/fuzz/"
                ];
              };
              # --- manager-owned languages: python, lua, ts, nix --------
              # git-third-party is a vendored helper tool (late imports by
              # design); the old engine skipped it (keyed on the `.py` ext),
              # so keep ruff off it.
              ruff = {
                enable = true;
                excludes = [ "^support/tools/git-third-party/" ];
              };
              ruff-format = {
                enable = true;
                excludes = [ "^support/tools/git-third-party/" ];
              };
              stylua = {
                enable = true;
                files = "\\.lua$";
              };
              prettier = {
                enable = true;
                files = "\\.(ts|tsx)$";
                excludes = [ "^\\.git-third-party" ];
              };
              nixfmt = {
                enable = true;
                files = "\\.nix$";
              };
              # --- rust (per-crate, edition-aware) ----------------------
              cargo-fmt = {
                enable = true;
                name = "cargo-fmt";
                entry = toString cargo-fmt-all;
                files = "\\.rs$";
                pass_filenames = false;
              };
              # --- github workflow / action schemas ---------------------
              check-github-workflows = {
                enable = true;
                name = "check-github-workflows";
                entry = "${pkgs.check-jsonschema}/bin/check-jsonschema --builtin-schema vendor.github-workflows";
                files = "^\\.github/workflows/.*\\.ya?ml$";
              };
              check-github-actions = {
                enable = true;
                name = "check-github-actions";
                entry = "${pkgs.check-jsonschema}/bin/check-jsonschema --builtin-schema vendor.github-actions";
                files = "^\\.github/(actions/.+/)?action\\.ya?ml$";
              };
              # --- local guards -----------------------------------------
              # Keep the manager crate's [package] version in lockstep with
              # .genvm-monorepo-root (executor lines version independently).
              check-cargo-versions = {
                enable = true;
                name = "check-cargo-versions";
                entry = "${hooks-python}/bin/python3 -B ./support/ci tool check-versions sync";
                files = "^(implementation/Cargo\\.toml|\\.genvm-monorepo-root)$";
                pass_filenames = false;
              };
              markdown-local-links = {
                enable = true;
                name = "markdown-local-links";
                entry = "${hooks-python}/bin/python3 support/scripts/md-local-links.py";
                types = [ "markdown" ];
              };
              # --- commit-msg -------------------------------------------
              check-commit-message = {
                enable = true;
                name = "check-commit-message";
                entry = "${hooks-python}/bin/python3 support/scripts/check-commit-message.py --message-file";
                stages = [ "commit-msg" ];
              };
            };
          };

          # ---- Active executor lines -------------------------------
          # `.genvm-monorepo-root` carries two independent versions:
          #   * `active-versions` — the executor lines built here; each
          #     line's manifest.json is the source of truth for its own
          #     executor-version.
          #   * `version`          — the manager's release version, which
          #     may differ from any executor line.
          monorepo = builtins.fromJSON (builtins.readFile ./.genvm-monorepo-root);

          host-system-as-genvm =
            {
              "x86_64-linux" = "amd64-linux";
              "aarch64-linux" = "arm64-linux";
              "aarch64-darwin" = "arm64-macos";
            }
            ."${system}";

          # v0.3.0-rc7 -> v0.3 : the major.minor line label used in
          # package names (`executor-<version>`, ...).
          clamp-version =
            v:
            let
              m = builtins.match "v?([0-9]+)\\.([0-9]+).*" v;
            in
            "v${builtins.elemAt m 0}.${builtins.elemAt m 1}";

          make-release-args =
            exec-src: exec-prefix:
            (import ./support {
              inherit
                pkgs
                deps
                system
                exec-prefix
                ;
              root-src = self;
              executor-src = exec-src;
            })
            // {
              inherit host-system-as-genvm exec-prefix;
              host-system = system;
            };

          # The manager is its own component: `version` is the manager's
          # release version (schema tag / GENVM_PROFILE) and is independent
          # of any executor line — they may differ (e.g. manager v0.6.0-rc0
          # shipping the v0.3 executor line). Its *sources*, though, come
          # from an active executor line's checkout, since it mounts that
          # line's executor crates (see implementation/default.nix). Use the
          # first active line for that.
          #
          # compiled-libs depend only on pkgs/deps/zig/lua-src, so this one
          # build is reused by every package.
          manager-version = monorepo.version;
          manager-line-prefix = "executors/${builtins.head monorepo.active-versions}.x";
          manager-line-src = self + "/${manager-line-prefix}";
          manager-release-args = make-release-args manager-line-src manager-line-prefix;

          compiled-libs = import ./libs manager-release-args;

          # One entry per active line: its clamped label + the built,
          # version-named executor package set (executor / -<platform>
          # rewritten to executor-<version> / executor-<version>-<platform>).
          executor-lines = builtins.map (
            key:
            let
              exec-prefix = "executors/${key}.x";
              exec-src = self + "/${exec-prefix}";
              manifest = builtins.fromJSON (builtins.readFile (exec-src + "/manifest.json"));
              clamped = builtins.replaceStrings [ "." ] [ "_" ] (clamp-version manifest.executor-version);
              release-args = make-release-args exec-src exec-prefix;
              # executors/<key>.x/default.nix reads its own
              # manifest.json for build-config (executor-version).
              raw = import "${exec-src}" (release-args // { inherit compiled-libs; });
            in
            {
              inherit clamped;
              packages = pkgs.lib.mapAttrs' (
                name: value:
                pkgs.lib.nameValuePair "executor-${clamped}${pkgs.lib.removePrefix "executor" name}" value
              ) raw;
            }
          ) monorepo.active-versions;

          executor-packages = builtins.foldl' (acc: line: acc // line.packages) { } executor-lines;

          # The manager is a single binary tagged with its own `version`
          # (independent of the executor lines it ships against).
          manager-packages =
            pkgs.lib.mapAttrs
              (
                _: package:
                package.overrideAttrs (_: {
                  # These are portable release trees, not Nix-only executables.
                  # Keep install/bin/genvm-manager's `#!/bin/sh` instead of
                  # rewriting it to a host Nix store path during fixup.
                  dontPatchShebangs = true;
                })
              )
              (
                import ./implementation (
                  manager-release-args
                  // {
                    inherit compiled-libs genvm-tool;
                    build-config = {
                      executor-version = manager-version;
                    };
                  }
                )
              );

          # ---- Runners ---------------------------------------------
          # The executor lines only export their own current runner
          # lists; ./runners accumulates them across lines and builds
          # them. We always build all of them (runners-all).
          runners-args = {
            inherit pkgs deps pkgs-overlays;
            root-src = self;
            host-system = system;
          };

          runners = import ./runners runners-args;

          runners-list = runners.list;

          # Legacy lines (v0.2.x) keep their runners private under the executor
          # root (executor/<version>/legacy-runners) rather than the shared
          # top-level runners/ dir: their registry hashes use Nix base32, a
          # different scheme from the forward-rolling lines, so the two runner
          # trees must not be pooled together.
          is-legacy-runner = r: pkgs.lib.hasPrefix "v0.2." r.version;
          shared-runners-list = builtins.filter (r: !is-legacy-runner r) runners-list;
          legacy-runners-list = builtins.filter is-legacy-runner runners-list;

          universal-of = import ./runners/views/universal.nix runners-args;

          # `cp -rsf src/. $out/` lays out a shadow tree: real directories and
          # store-backed symlinks for regular files. Existing relative file
          # symlinks are then restored verbatim so links such as
          # `bin/post-install -> genvm-post-install` remain portable. It merges
          # the top-level trees WITHOUT copying, and `-f` keeps the old
          # last-one-wins overwrite semantics. Real files are materialised only
          # later by the artifact packer.
          symlink-merge-srcs = ''
            for src in $srcs; do
              cp -rsf --no-preserve=mode,ownership "$src"/. $out/
              while IFS= read -r -d "" link; do
                if [ -d "$link" ]; then
                  echo "refusing to create directory symlink: $link" >&2
                  exit 1
                fi
                target="$(readlink "$link")"
                if [[ "$target" != /* ]]; then
                  rel="''${link#"$src"/}"
                  ln -sfn "$target" "$out/$rel"
                fi
              done < <(find "$src" -type l -print0)
            done
          '';

          # Merge a { uid -> `<id>/<aa>/<rest>.tar` tree } set into one tree.
          merge-runner-trees =
            name: uni:
            pkgs.runCommand name { srcs = builtins.attrValues uni; } ''
              mkdir -p $out
              ${symlink-merge-srcs}
            '';

          runners-all = merge-runner-trees "genvm-runners-all" (universal-of shared-runners-list);

          # The legacy runners, laid out at their final relative destination
          # `executor/<version>/legacy-runners/<id>/<aa>/<rest>.tar` so consumers
          # only have to overlay this tree onto their output root.
          legacy-runners-all = pkgs.stdenvNoCC.mkDerivation {
            name = "genvm-legacy-runners-all";
            dontUnpack = true;
            dontConfigure = true;
            dontBuild = true;
            dontFixup = true;
            installPhase = ''
              mkdir -p $out
            ''
            + pkgs.lib.concatMapStrings (
              r:
              let
                uidMatch = builtins.match "([^:]+):(.+)" r.uid;
                hash32 =
                  if uidMatch == null then
                    throw "invalid runner uid `${r.uid}`; expected `<prefix>:<hash32>`"
                  else
                    builtins.elemAt uidMatch 1;
                dst = "executor/${r.version}/legacy-runners/${r.id}/${builtins.substring 0 2 hash32}/${builtins.substring 2 50 hash32}.tar";
              in
              ''
                mkdir -p "$out/$(dirname -- "${dst}")"
                ln -s "${r.derivation}" "$out/${dst}"
              ''
            ) legacy-runners-list;
          };

          # ---- Combined genvm distribution -------------------------
          # `genvm` (and per-platform variants) merges every active
          # line's executor, the manager, and all runners into one tree.
          platform-suffixes = [
            ""
            "-amd64-linux"
            "-arm64-linux"
            "-arm64-macos"
          ];
          combine-genvm =
            suffix:
            let
              execs = builtins.map (line: executor-packages."executor-${line.clamped}${suffix}") executor-lines;
              manager = manager-packages."manager${suffix}";
              # executors + manager land at the top level; runners-all is
              # nested under `runners/` (matching the CI build/out layout).
              srcs = execs ++ [ manager ];
            in
            pkgs.stdenvNoCC.mkDerivation {
              name = "genvm${suffix}";
              inherit srcs;
              runnersAll = runners-all;
              legacyRunnersAll = legacy-runners-all;
              dontUnpack = true;
              dontConfigure = true;
              dontBuild = true;
              dontFixup = true;
              installPhase = ''
                mkdir -p $out $out/runners
                ${symlink-merge-srcs}
                # runners-all nested under runners/ (matching the CI layout).
                cp -rsf "$runnersAll"/. $out/runners/
                # Legacy lines carry their runners under their own executor root
                # (executor/<version>/legacy-runners); overlay that tree here.
                cp -rsf "$legacyRunnersAll"/. $out/
              '';
            };
          genvm-packages = builtins.listToAttrs (
            builtins.map (
              suffix: pkgs.lib.nameValuePair "genvm${suffix}" (combine-genvm suffix)
            ) platform-suffixes
          );

          # ---- Release distribution bundles -------------------------
          # `executor[-<platform>]` (no version) merges every active line's
          # executor tree (executor/<version>/...) into one bundle. The release
          # publishes each line on its own (`executor-<version>[-<platform>]`,
          # one release per line in the executor repo), so this merged bundle has
          # no CI consumer; it stays as the convenient "give me every line"
          # target for local use. Runners are NOT included; they ship
          # platform-independently via artifact-prepack-genvm-universal.
          combine-executors =
            suffix:
            pkgs.runCommand "genvm-executor${suffix}"
              {
                srcs = builtins.map (line: executor-packages."executor-${line.clamped}${suffix}") executor-lines;
              }
              ''
                mkdir -p $out
                ${symlink-merge-srcs}
              '';
          executor-dist-packages = builtins.listToAttrs (
            builtins.map (
              suffix: pkgs.lib.nameValuePair "executor${suffix}" (combine-executors suffix)
            ) platform-suffixes
          );

          # Trees consumed by the artifact packer. Platform artifacts contain
          # the manager and every active executor line for that platform. The
          # universal artifact contains the platform-independent runners at
          # their final `runners/` destination, plus the legacy lines' runners at
          # theirs. Keep every leaf as a symlink to its component derivation so
          # prepacking does not duplicate outputs.
          artifact-prepack-platforms = [
            "amd64-linux"
            "arm64-linux"
            "arm64-macos"
          ];
          artifact-prepack-platform-packages = builtins.listToAttrs (
            builtins.map (
              platform:
              let
                suffix = "-${platform}";
              in
              pkgs.lib.nameValuePair "artifact-prepack-genvm-${platform}" (
                pkgs.runCommand "artifact-prepack-genvm-${platform}"
                  {
                    srcs =
                      builtins.map (line: executor-packages."executor-${line.clamped}${suffix}") executor-lines
                      ++ [ manager-packages."manager${suffix}" ];
                  }
                  ''
                    mkdir -p $out
                    ${symlink-merge-srcs}
                  ''
              )
            ) artifact-prepack-platforms
          );
          artifact-prepack-genvm-universal =
            pkgs.runCommand "artifact-prepack-genvm-universal"
              {
                runnersAll = runners-all;
                legacyRunnersAll = legacy-runners-all;
              }
              ''
                mkdir -p $out/runners
                cp -rsf "$runnersAll"/. $out/runners/
                # Legacy lines carry their runners under their own executor root
                # (executor/<version>/legacy-runners), already laid out at that
                # destination — overlay the tree as-is.
                cp -rsf "$legacyRunnersAll"/. $out/
              '';
          artifact-prepack-packages = artifact-prepack-platform-packages // {
            inherit artifact-prepack-genvm-universal;
          };
        in
        {
          runners = runners-list;
          checks.pre-commit-check = pre-commit-check;
          # `nix fmt` runs the same generated hook config as `nix flake check`,
          # in the working tree (manager files only — executors have their own).
          formatter = pkgs.writeShellScriptBin "pre-commit-run" ''
            exec ${pre-commit-check.config.package}/bin/pre-commit run --all-files --config ${pre-commit-check.config.configFile}
          '';
          packages =
            # Combined distribution: all executors + manager + runners.
            genvm-packages
            # Per-line executors: executor-<version>[-<platform>].
            // executor-packages
            # All active lines merged: executor[-<platform>] (release bundles).
            // executor-dist-packages
            # Manager: manager[-<platform>].
            // manager-packages
            // {
              inherit runners-all legacy-runners-all;
            }
            # Inputs for producing the three platform artifacts and the
            # platform-independent universal artifact.
            // artifact-prepack-packages
            # Utility packages (grouped separately).
            // {
              inherit genvm-tool;
            };

          devShells.minimal = pkgs.mkShell {
            packages = packages-0 ++ packages-py-test ++ [ pkgs.ruby ];
          };
          devShells.py-test = pkgs.mkShell {
            packages = packages-py-test ++ [ pkgs.ruby ];
            shellHook = shell-hook-base + ''
              export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:${toString pkgs.zlib.out}/lib:''${LD_LIBRARY_PATH:-}"
            '';
          };
          devShells.gen-docs = pkgs.mkShell {
            # Self-contained docs toolchain (sphinx + extensions via nix, no
            # poetry). generate.py also runs `nix eval`, so keep the base tools.
            packages = packages-0 ++ packages-gen-docs;
            shellHook = shell-hook-base;
          };
          devShells.initial-check = pkgs.mkShell {
            packages = packages-0 ++ packages-rust;
            shellHook = shell-hook-base;
          };
          devShells.rust-test = pkgs.mkShell {
            packages = packages-0 ++ packages-debug-test ++ packages-rust;
            shellHook = shell-hook-base + ''
              export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:${toString pkgs.zlib.out}/lib:''${LD_LIBRARY_PATH:-}"
            '';
          };
          devShells.mock-tests = pkgs.mkShell {
            packages = packages-0 ++ packages-rust ++ packages-debug-test;
            shellHook = shell-hook-base;
          };
          devShells.full = pkgs.mkShell {
            packages =
              packages-0
              ++ packages-debug-test
              ++ packages-py-test
              ++ packages-rust
              ++ packages-gen-docs
              ++ [ pkgs.nodejs ]
              # pre-commit + the hook tools, so `pre-commit run` works in-shell.
              # (No rustfmt/cargo here — cargo-fmt is a custom hook — so the
              # custom-rust toolchain is not shadowed.)
              ++ pre-commit-check.enabledPackages;
            # The interactive dev shell (.envrc → `.#full`) also installs the
            # git-hooks pre-commit/commit-msg stubs into .git/hooks.
            shellHook = shell-hook-base + pre-commit-check.shellHook;
          };
          devShells.check-qemu = pkgs.mkShell {
            packages = packages-0 ++ [ pkgs.qemu ];
            shellHook = shell-hook-base;
          };
        }
      );
    in
    for-systems;
}
