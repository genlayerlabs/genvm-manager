# The runner manifest shipped in one executor's `data/` directory, as a Nix
# value (the release view / ninja eval turn it into `all.json`):
#
#   { <id> = {
#       latest = <hash32>;   the executor's own current runner
#       versions = { <hash32> = { forks; supersedes; }; };
#   }; }
#
# `versions` holds every runner compatible up to the executor's version and is
# what the executor consults to accept a requested runner hash; `latest`
# resolves an id in debug mode. `forks` is the `|`-joined, sorted set of forks
# of every line declaring that hash: the runner is visible iff any is active.
{
  executorVersion,
  root-src ? ../.,
  ...
}@args:
let
  api = import ./default.nix args;

  chainforks = builtins.fromJSON (builtins.readFile (root-src + "/support/genvm-chainforks.json"));

  hash32 = x: builtins.head (builtins.tail (builtins.match "([^:]+):(.*)" x.uid));

  own = builtins.filter (
    runner: runner.version == executorVersion && !(runner.archived or false)
  ) api.list;
  compatible = api.forExecutor executorVersion;

  latest = builtins.foldl' (
    acc: x:
    if acc ? ${x.id} then
      throw "runner ${x.id} ships 2 current runners for ${executorVersion}: ${acc.${x.id}} and ${hash32 x}"
    else
      acc // { ${x.id} = hash32 x; }
  ) { } own;

  checkFork =
    x:
    if !(chainforks.forks ? ${x.fork}) then
      throw "runner ${x.uid} of ${x.version}: fork `${x.fork}` is not defined in support/genvm-chainforks.json"
    else
      x.fork;

  # <id> -> <hash32> -> { supersedes; forks = <line version> -> <fork>; }
  declarations = builtins.foldl' (
    acc: x:
    let
      byId = acc.${x.id} or { };
      h = hash32 x;
      existing =
        byId.${h} or {
          inherit (x) supersedes;
          forks = { };
        };
      fork = checkFork x;
    in
    if existing.supersedes != x.supersedes then
      throw "runner ${x.uid} is pinned with different supersedes across executor lines"
    else if existing.forks ? ${x.version} && existing.forks.${x.version} != fork then
      throw "runner ${x.uid} is declared twice by ${x.version} with different forks"
    else
      acc
      // {
        ${x.id} = byId // {
          ${h} = existing // {
            forks = existing.forks // {
              ${x.version} = fork;
            };
          };
        };
      }
  ) { } compatible;

  versions = builtins.mapAttrs (
    _id:
    builtins.mapAttrs (
      _h: d: {
        inherit (d) supersedes;
        # attrNames is sorted and unique
        forks = builtins.concatStringsSep "|" (
          builtins.attrNames (
            builtins.listToAttrs (
              builtins.map (f: {
                name = f;
                value = null;
              }) (builtins.attrValues d.forks)
            )
          )
        );
      }
    )
  ) declarations;
in
builtins.mapAttrs (
  id: vs:
  let
    unlisted = builtins.filter (m: m.supersedes != null && !(vs ? ${m.supersedes})) (
      builtins.attrValues vs
    );
    selfSuperseding = builtins.filter (h: vs.${h}.supersedes == h) (builtins.attrNames vs);
  in
  if !(latest ? ${id}) then
    throw "runner ${id} ships no current runner for ${executorVersion}"
  else if selfSuperseding != [ ] then
    throw "runner ${id}:${builtins.head selfSuperseding} supersedes itself"
  else if unlisted != [ ] then
    throw "runner ${id} supersedes unlisted ${(builtins.head unlisted).supersedes}"
  else
    {
      latest = latest.${id};
      versions = vs;
    }
) versions
