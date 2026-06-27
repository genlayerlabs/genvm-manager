# Docs view for docs/website/generate.py: maps every accumulated runner to
#   { <executor-version> = { <id> = [ <hash32> ]; }; }
# Each runner already carries the version of the executor line it came from, so
# the grouping is just by `version` (no git-history / commit-to-tag lookup).
{
  ...
}:
let
  list = import ./all.nix { };

  hash32 =
    r:
    if r.hash == "test" then
      "vTEST"
    # gvm32 (Crockford Base32) — the encoding the executor uses for runner
    # paths. Extracted from `uid` (`id:gvm32hash`); NOT Nix base32.
    else
      builtins.head (builtins.match "[^:]+:(.*)" r.uid);

  res = builtins.foldl' (
    acc: r:
    let
      byId = acc.${r.version} or { };
      hashes = byId.${r.id} or { };
    in
    acc
    // {
      ${r.version} = byId // {
        ${r.id} = hashes // {
          ${hash32 r} = true;
        };
      };
    }
  ) { } list;
in
builtins.mapAttrs (_version: builtins.mapAttrs (_id: builtins.attrNames)) res
