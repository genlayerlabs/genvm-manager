# Every fork a runner of an active executor line names, sorted and unique:
#   [ <fork> ]
args@{ ... }:
builtins.attrNames (
  builtins.listToAttrs (
    builtins.map (runner: {
      name = runner.fork;
      value = null;
    }) (import ./all.nix args)
  )
)
