# The runner manifests shipped in one executor's `data/` directory, as Nix
# values (the release view / ninja eval turn these into JSON files):
#
#   latest : { <id> = <hash32>;   }  the executor's own current runners
#   all    : { <id> = [ <hash32> ]; } every runner compatible up to its version
#
# `all` is what the executor consults to accept a requested runner hash; `latest`
# resolves an id to the executor's own newest runner (used in debug mode).
{
  executorVersion,
  ...
}@args:
let
  api = import ./default.nix args;

  hash32 = x: builtins.head (builtins.tail (builtins.match "([^:]+):(.*)" x.uid));

  own = builtins.filter (runner: runner.version == executorVersion) api.list;
  compatible = api.forExecutor executorVersion;

  single =
    runners:
    builtins.listToAttrs (
      builtins.map (x: {
        name = x.id;
        value = hash32 x;
      }) runners
    );

  multi =
    runners:
    builtins.foldl' (
      acc: x:
      let
        existing = acc.${x.id} or [ ];
        h = hash32 x;
      in
      acc // { ${x.id} = if builtins.elem h existing then existing else existing ++ [ h ]; }
    ) { } runners;
in
{
  latest = single own;
  all = multi compatible;
}
