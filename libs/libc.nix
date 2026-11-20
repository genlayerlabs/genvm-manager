{
  conf-target,
  name-target,
  pkgs,
  deps,
  ...
}:
let
  musl-src = deps."musl-1.2.6";
  aarch64-compiler-rt = deps."compiler-rt-aarch64-21.1.5-1";
in
pkgs.stdenvNoCC.mkDerivation {
  name = "genvm-libc-${name-target}";

  src = musl-src;

  nativeBuildInputs = [
    pkgs.clang
    pkgs.lld
    pkgs.coreutils
  ];

  configurePhase =
    (
      if name-target == "arm64" then
        ''
          cp -r ${aarch64-compiler-rt}/. ./compiler-rt
        ''
      else
        ""
    )
    + ''
      CC="clang --target=${conf-target}-linux-musl" \
      CFLAGS="-O2" \
      LDFLAGS="-fuse-ld=lld ${
        if name-target == "arm64" then
          "./compiler-rt/usr/lib/clang/21/lib/linux/libclang_rt.builtins-aarch64.a"
        else
          ""
      }" \
      ./configure --target=${conf-target}
    '';

  buildPhase = ''
    make -j lib/libc.so
  '';

  installPhase = ''
    mkdir -p "$out/lib"
    cp lib/libc.so "$out/lib/libc.so"
  '';
}
