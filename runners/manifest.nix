# The runner manifest shipped in one executor's `data/` directory, as a Nix
# value (the release view / ninja eval turn it into `all.json`):
#
#   { <id> = {
#       latest = <hash32>;   the executor's own current runner
#       versions = { <hash32> = { available_after; supersedes; }; };
#   }; }
#
# `versions` holds every runner compatible up to the executor's version and is
# what the executor consults to accept a requested runner hash; `latest`
# resolves an id in debug mode.
{
  executorVersion,
  ...
}@args:
let
  api = import ./default.nix args;

  hash32 = x: builtins.head (builtins.tail (builtins.match "([^:]+):(.*)" x.uid));

  own = builtins.filter (
    runner: runner.version == executorVersion && !(runner.archived or false)
  ) api.list;
  compatible = api.forExecutor executorVersion;

  latest = builtins.listToAttrs (
    builtins.map (x: {
      name = x.id;
      value = hash32 x;
    }) own
  );

  timestampRe = "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})";

  meta =
    x:
    if x.available-after != null && builtins.match timestampRe x.available-after == null then
      throw "runner ${x.uid}: available-after `${x.available-after}` is not an RFC 3339 timestamp"
    else
      {
        available_after = x.available-after;
        inherit (x) supersedes;
      };

  versions = builtins.foldl' (
    acc: x:
    let
      existing = acc.${x.id} or { };
      h = hash32 x;
    in
    if existing ? ${h} && existing.${h} != meta x then
      throw "runner ${x.uid} is pinned with different metadata across executor lines"
    else
      acc
      // {
        ${x.id} = existing // {
          ${h} = meta x;
        };
      }
  ) { } compatible;
in
builtins.mapAttrs (
  id: vs:
  let
    unlisted = builtins.filter (m: m.supersedes != null && !(vs ? ${m.supersedes})) (
      builtins.attrValues vs
    );
  in
  if !(latest ? ${id}) then
    throw "runner ${id} ships no current runner for ${executorVersion}"
  else if unlisted != [ ] then
    throw "runner ${id} supersedes unlisted ${(builtins.head unlisted).supersedes}"
  else
    {
      latest = latest.${id};
      versions = vs;
    }
) versions
