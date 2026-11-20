# Every executor line, resolved from the umbrella manifest into
#   [ { version; fork; src; } ]
# where `version` is the line's pinned `executor-version`, `fork` the chainfork
# gating it, and `src` its checkout. Each checkout's `runners` directory exports
# that line's current runner list. This — not git history — is where the runner
# history lives: every executor line contributes its own runners.
{
  root-src ? ../.,
  ...
}:
let
  monorepo = builtins.fromJSON (builtins.readFile (root-src + "/.genvm-monorepo-root"));
in
builtins.map (
  key:
  let
    exec-rel = "executors/${key}.x";
    manifest = builtins.fromJSON (builtins.readFile (root-src + "/${exec-rel}/manifest.json"));
  in
  {
    version = manifest.executor-version;
    inherit (manifest) fork;
    src = root-src + "/${exec-rel}";
  }
) (builtins.attrNames monorepo.executor-lines)
