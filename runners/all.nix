# The accumulated list of every runner across every active executor line, each
# tagged with the `version` of the line it came from:
#   [ { id; hash; uid; derivation; version; } ]
{
  host-system ? "x86_64-linux",
  deps ? null,
  pkgs-overlays ? [ ],
  root-src ? ../.,
  ...
}:
let
  executors = import ./executors.nix { inherit root-src; };
in
builtins.concatMap (
  exec:
  builtins.map (runner: runner // { inherit (exec) version; }) (
    import (exec.src + "/runners") { inherit host-system deps pkgs-overlays; }
  )
) executors
