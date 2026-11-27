# Writes the per-executor runner manifest (see ../manifest.nix) to a JSON file
# for the executor build to drop into its `data/` directory.
{
  executorVersion,
  ...
}@args:
{
  all = builtins.toFile "all.json" (builtins.toJSON (import ../manifest.nix args));
}
