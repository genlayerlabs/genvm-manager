# Getting Runners

Runners build only on x86_64 Linux — `ninja -C build all/runners`, or the
`#runners-all` nix attribute. Never build them natively on macOS — deterministic
runner artifacts require a Linux builder; use a remote nix builder or download:

```bash
python3 build/out/bin/genvm-post-install \
    --default-steps false --runners-download true --error-on-missing-executor false
```

Alternatively fetch `genvm-universal.tar.xz` from a
[genvm-manager release](https://github.com/genlayerlabs/genvm-manager/releases)
and extract it over `build/out`

Changing a runner: [modify-runner.md](../extending/modify-runner.md)
