# Writes the per-executor runner manifests (see ../manifest.nix) to JSON files
# for the executor build to drop into its `data/` directory.
{
  executorVersion,
  ...
}@args:
let
  manifest = import ../manifest.nix args;
in
{
  latest = builtins.toFile "latest.json" (builtins.toJSON manifest.latest);
  all = builtins.toFile "all.json" (builtins.toJSON manifest.all);
}
