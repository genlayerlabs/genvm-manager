# Getting Runners

Runners build only on x86_64 Linux — `ninja -C build all/runners`, or the
`#runners-all` nix attribute. Never build them natively on macOS — deterministic
runner artifacts require a Linux builder; use a remote nix builder or download
`genvm-universal.tar.xz` from a
[genvm-manager release](https://github.com/genlayerlabs/genvm-manager/releases)
and extract it over `build/out`

`genvm-post-install` downloads nothing: runners reach an install only through
that asset

Changing a runner: [modify-runner.md](../extending/modify-runner.md)
