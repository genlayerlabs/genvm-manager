{
  pkgs,
  root-src,
  compile-rust,
  get-root-subtree,
  build-config,
  patch-yaml-schema,
  genvm-tool,
  patch-llm-config,
  patch-web-config,
  patch-rpath,
  host-system,
  host-system-as-genvm,
  compiled-libs,
  ...
}@args:
let
  lib = pkgs.lib;

  monorepo = builtins.fromJSON (builtins.readFile (root-src + "/.genvm-monorepo-root"));
  release-src = get-root-subtree (
    [
      ".genvm-monorepo-root"
      "install"
      "libs/unhardcoded-engine/llm_policy"
      "libs/unhardcoded-engine/llm_policy.lua"
      "support/manifest-base.yaml"
    ]
    ++ builtins.map (line: "executors/${line}.x/manifest.json") monorepo.active-versions
  );

  # The shipped LLM dispatch script requires the `llm_policy` package, which
  # lives in the unhardcoded-engine submodule instead of under install/.
  llm-policy-src = release-src + "/libs/unhardcoded-engine";

  make-for-target =
    target:
    let
      exe = compile-rust rec {
        inherit target;
        pname = "genvm-modules-bin";
        version = "0.1.0";

        profile = "release-with-debug";

        cargoLock.lockFile = ./Cargo.lock;

        src = get-root-subtree [
          "implementation"
          "crates/modules-interfaces"
          "crates/calldata"
          "crates/calldata-derive"
          # a dev-dependency of the executor crates below, but cargo still needs its manifest
          "crates/fuzzing"
          # executor crates come from the v0.3.x submodule mount
          "executors/v0.3.x/executor/crates"
        ];
        sourceRoot = "./source/implementation";

        extraLibs = compiled-libs.${target};

        LUA_LIB_NAME = "lua";

        LSQLITE3_PREBUILT = "1";

        GENVM_PROFILE = build-config.executor-version;
      };
    in
    pkgs.stdenvNoCC.mkDerivation rec {
      name = "genvm-manager-${target}";

      srcs = [
        exe
        (release-src + "/install")
      ]
      ++ compiled-libs.${target};

      dontUnpack = true;
      dontConfigure = true;
      dontBuild = true;

      nativeBuildInputs = [
        pkgs.makeWrapper
        patch-yaml-schema
        genvm-tool
        patch-llm-config
        patch-web-config
        patch-rpath
      ];

      installPhase = ''
        mkdir -p $out/bin
        mkdir -p $out/lib
        cp ${exe} "$out/bin/genvm-modules"
        for src in $srcs; do
        if [[ "$src" != "${exe}" ]]
        then
        cp --no-preserve=ownership -r "$src/." "$out/."
        chmod -R u+w "$out"
        fi
        done
        # compiled-libs doubles as the link input and as a copy source, so its
        # static archives land here too; they are already inside the binary.
        rm -f "$out"/lib/*.a

        if [ ! -f "${llm-policy-src}/llm_policy.lua" ]; then
          echo "${llm-policy-src} is missing or incomplete; run \`git submodule update --init libs/unhardcoded-engine\`" >&2
          exit 1
        fi
        cp --no-preserve=ownership "${llm-policy-src}/llm_policy.lua" "$out/lib/genvm-lua/llm_policy.lua"
        cp --no-preserve=ownership -r "${llm-policy-src}/llm_policy" "$out/lib/genvm-lua/llm_policy"
        chmod -R u+w "$out/lib/genvm-lua"

        patch-yaml-schema --tag ${build-config.executor-version} "$out"
        patch-llm-config --tag ${build-config.executor-version} "$out/config/genvm-module-llm.yaml"
        patch-web-config --tag ${build-config.executor-version} "$out/config/genvm-module-web.yaml"

        # Assemble data/manifest.yaml from the active executor submodules
        # (executor-version + available-after) and the static base fields.
        genvm-tool -C ${release-src} build-manifest --output "$out/data/manifest.yaml"

        patch-rpath --codesign --search-dir "$out/lib" --rpath '$ORIGIN/../lib' "$out/bin/genvm-modules"
        find "$out/lib" -type f -name '*.so' -not -name 'libc.so' | while read lib; do
        patch-rpath --search-dir "$out/lib" --rpath '$ORIGIN' "$lib"
        done
      '';
    };
in
{
  manager = make-for-target host-system-as-genvm;
  manager-amd64-linux = make-for-target "amd64-linux";
  manager-arm64-linux = make-for-target "arm64-linux";
  manager-arm64-macos = make-for-target "arm64-macos";
}
