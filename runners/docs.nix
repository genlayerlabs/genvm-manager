# Docs view for docs/website/generate.py: every accumulated runner as
#   [ { id; hash32; version; fork; supersedes; from-source; } ]
# `from-source` marks a runner the line builds from its own tree (it has a pin
# hash), as opposed to a legacy one the line only ships by uid
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
in
builtins.map (r: {
  inherit (r) id version;
  hash32 = hash32 r;
  fork = r.fork or null;
  supersedes = r.supersedes or null;
  from-source = (r.hash or null) != null;
}) list
