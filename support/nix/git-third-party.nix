# Library for genvm's `.git-third-party` convention.
#
# A `.git-third-party` directory holds a `manifest.json`:
#   { "repos": { "<repo-path>": { "url": ..., "commit": ..., "patches": [<name>...] } } }
# and patch files at `patches/<repo-path>/<name>`, applied in the listed order.
# Older trees name the manifest `config.json` and carry a patch *count* there,
# the files being `1`..`<count>`; both are read.
#
# `load <dir>` fetches each repo at its pinned commit, applies its patches, and
# returns an attrset { <repo-path> = <patched source derivation>; }. Callers
# decide where (if anywhere) to mount the results.
{ pkgs }:
let
  load =
    dir:
    let
      manifest =
        let
          current = "${dir}/manifest.json";
        in
        if builtins.pathExists current then current else "${dir}/config.json";
      repos = (builtins.fromJSON (builtins.readFile manifest)).repos;
    in
    builtins.mapAttrs (
      path: cfg:
      let
        unpatched = builtins.fetchGit {
          url = cfg.url;
          rev = cfg.commit;
          shallow = true;

          name = "gtt-" + builtins.hashString "sha256" path + "-unpatched";
        };
      in
      pkgs.applyPatches {
        name = "gtt-" + builtins.hashString "sha256" path + "-patched";
        src = unpatched;
        patches = builtins.map (name: "${dir}/patches/${path}/${name}") (
          if builtins.isInt cfg.patches then
            builtins.genList (patch-no: toString (patch-no + 1)) cfg.patches
          else
            cfg.patches
        );
      }
    ) repos;
in
{
  inherit load;
}
