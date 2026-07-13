# Runners

Runners build only on x86_64 Linux (part of the `all` ninja target /
`#runners-all` nix attr). On other hosts, download them:

```bash
python3 build/out/bin/post-install.py --create-venv false --default-step false --runners-download true --error-on-missing-executor false
```

or fetch `genvm-runners-all.tar.xz` from a
[genvm-manager release](https://github.com/genlayerlabs/genvm-manager/releases)
and extract it over `build/out`.

Do **not** build runners natively on macOS — deterministic runner artifacts
require a Linux builder (remote nix builder or download).

Changing a runner: [modify-runner.md](../extending/modify-runner.md).
