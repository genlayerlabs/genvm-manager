# Release / nix build

Nix flake packages see only **committed** submodule content — commit inside
submodules first ([submodules.md](../committing/submodules.md)), and always pass
`?submodules=1`.

Platform suffixes: none (native), `-amd64-linux`, `-arm64-linux`, `-arm64-macos`.

| Package | Contents |
|---|---|
| `genvm[-<platform>]` | everything: manager + all executor lines + runners |
| `manager[-<platform>]` | manager bundle (`bin/`, `lib/`, `config/`, `data/`) |
| `executor[-<platform>]` | all active executor lines merged (`executor/<version>/…`) |
| `executor-<version>[-<platform>]` | one executor line |
| `artifact-prepack-genvm-<platform>` | a platform's release asset: manager + every active executor line |
| `artifact-prepack-genvm-universal` | the universal release asset: runners (`runners/` prefix + legacy-line overlay) |

```bash
nix build -v -L '.?submodules=1#genvm-amd64-linux'
```

## Release assets

`.github/workflows/release.yaml` is `incl_release_build_test.yaml` (plan → build
every asset → test every platform) plus a publisher. Everything is built here —
the executor is not a standalone build entry point.

Assets are named with the same platform token as the nix targets
(`amd64-linux`, …), not a reversed one. Each is one prepacked tree, and the
tarball is already named as the asset, so nothing renames it on the way out:

| Asset | Package |
|---|---|
| `genvm-<platform>.tar.xz` | `artifact-prepack-genvm-<platform>` |
| `genvm-universal.tar.xz` | `artifact-prepack-genvm-universal` |

All four go onto one **genvm-manager** release, tagged with `version` from
`.genvm-monorepo-root` (see [versioning.md](versioning.md)). They overlay onto a
single install root, so a full install is one platform asset + `genvm-universal`.

To exercise the whole pipeline on a PR without releasing anything, add the
`test-release-pipeline` label: `queue.yaml` then runs the same
`incl_release_build_test.yaml`. It also generates the release notes and prints
them, so they can be checked before the release.
