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
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      systems,
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
          packages-gen-docs = with pkgs; [
            lua-language-server
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
          '';

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
              clamped = clamp-version manifest.executor-version;
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
          manager-packages = import ./implementation (
            manager-release-args
            // {
              inherit compiled-libs genvm-tool;
              build-config = {
                executor-version = manager-version;
              };
            }
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

          runners-universal-set = runners.universal;

          runners-all = pkgs.stdenvNoCC.mkDerivation {
            name = "genvm-runners-all";
            srcs = builtins.attrValues runners-universal-set;
            dontUnpack = true;
            dontConfigure = true;
            dontBuild = true;
            dontFixup = true;
            installPhase = ''
              mkdir -p $out
              for src in $srcs; do
              cp --no-preserve=ownership -r $src/. $out/.
              chmod -R u+w $out
              done
            '';
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
              srcs = execs ++ [
                manager
                runners-all
              ];
            in
            pkgs.stdenvNoCC.mkDerivation {
              name = "genvm${suffix}";
              inherit srcs;
              dontUnpack = true;
              dontConfigure = true;
              dontBuild = true;
              dontFixup = true;
              installPhase = ''
                mkdir -p $out
                for src in $srcs; do
                cp --no-preserve=ownership -r $src/. $out/.
                chmod -R u+w $out
                done
              '';
            };
          genvm-packages = builtins.listToAttrs (
            builtins.map (
              suffix: pkgs.lib.nameValuePair "genvm${suffix}" (combine-genvm suffix)
            ) platform-suffixes
          );
        in
        {
          runners = runners-list;
          packages =
            # Combined distribution: all executors + manager + runners.
            genvm-packages
            # Per-line executors: executor-<version>[-<platform>].
            // executor-packages
            # Manager: manager[-<platform>].
            // manager-packages
            // {
              inherit runners-all;
            }
            # Utility packages (grouped separately).
            // {
              inherit genvm-tool;
            };

          devShells.py-test = pkgs.mkShell {
            packages = packages-py-test ++ [ pkgs.ruby ];
            shellHook = shell-hook-base + ''
              export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:${toString pkgs.zlib.out}/lib:''${LD_LIBRARY_PATH:-}"
            '';
          };
          devShells.gen-docs = pkgs.mkShell {
            packages = packages-py-test ++ packages-gen-docs ++ [ pkgs.ruby ];
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
              ++ [ pkgs.nodejs ];
            shellHook = shell-hook-base;
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
