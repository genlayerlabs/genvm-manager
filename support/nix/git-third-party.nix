# Library for genvm's `.git-third-party` convention.
#
# A `.git-third-party` directory holds a `config.json`:
#   { "repos": { "<repo-path>": { "url": ..., "commit": ..., "patches": <count> } } }
# and patch files at `patches/<repo-path>/<n>` (1-based).
#
# `load <dir>` fetches each repo at its pinned commit, applies its patches, and
# returns an attrset { <repo-path> = <patched source derivation>; }. Callers
# decide where (if anywhere) to mount the results.
{ pkgs }:
let
  load =
    dir:
    let
      repos = (builtins.fromJSON (builtins.readFile "${dir}/config.json")).repos;
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
        patches = builtins.genList (
          patch-no: "${dir}/patches/${path}/${toString (patch-no + 1)}"
        ) cfg.patches;
      }
    ) repos;
in
{
  inherit load;
}
