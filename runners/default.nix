# Umbrella runner machinery. The executor lines only export their own current
# runner lists; everything that accumulates them across lines, decides which
# runners a given executor may ship, and builds them lives here.
#
# Required args: pkgs, host-system. Optional: deps, pkgs-overlays, root-src.
args@{ ... }:
let
  # version -> version -> bool; the swappable compatibility policy.
  compatible = import ./compat.nix;

  # [ { version; src; } ] — the active executor lines.
  executors = import ./executors.nix args;

  # every runner across every line, tagged with its line's version.
  list = import ./all.nix args;

  # the runners an executor pinned at `executorVersion` may ship.
  forExecutor =
    executorVersion: builtins.filter (runner: compatible runner.version executorVersion) list;
in
{
  inherit
    compatible
    executors
    list
    forExecutor
    ;
}
