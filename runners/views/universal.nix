# Builds a runner list into a `runners/<id>/<aa>/<rest>.tar` tree, one derivation
# per uid (so identical runners shared across executor lines de-duplicate).
{
  pkgs,
  ...
}:
runners:
builtins.foldl' (
  acc: x:
  acc
  // {
    "${x.uid}" = pkgs.stdenvNoCC.mkDerivation {
      name = "genvm-runner-${x.uid}";
      src = x.derivation;
      dontUnpack = true;
      dontConfigure = true;
      dontBuild = true;
      dontFixup = true;
      installPhase =
        let
          uidMatch = builtins.match "([^:]+):(.+)" x.uid;
          hash32 =
            if uidMatch == null then
              throw "invalid runner uid `${x.uid}`; expected `<prefix>:<hash32>`"
            else
              builtins.elemAt uidMatch 1;
          # `<id>/<aa>/<rest>.tar` — consumers place this tree *under* their own
          # `runners/` dir, so do NOT prefix another `runners/` here.
          result-path = "${x.id}/${builtins.substring 0 2 hash32}/${builtins.substring 2 50 hash32}.tar";
        in
        ''
          mkdir -p $out/$(dirname -- ${result-path})
          cp ${x.derivation} "$out/${result-path}"
        '';
    };
  }
) { } runners
