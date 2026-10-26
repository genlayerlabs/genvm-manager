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
    # `hash` is the dev-mode marker, and only the forward-rolling lines carry it:
    # the legacy v0.2 line exposes just `uid`, so this must not assume it exists.
    if (r.hash or null) == "test" then
      "vTEST"
    # The hash the executor actually uses for the runner's on-disk path, taken
    # from `uid` (`id:hash32`) — gvm32 (Crockford Base32) on the forward lines,
    # Nix base32 on the legacy one. Either way `uid` is the source of truth.
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
