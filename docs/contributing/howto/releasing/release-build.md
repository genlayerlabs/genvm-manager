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
| `runners-all-dist` | platform-independent runners (`runners/` prefix + legacy-line overlay) |
| `genvm-tool` | the tool itself |

```bash
nix build -v -L '.?submodules=1#genvm-amd64-linux'
```

Release assets (`.github/workflows/release.yaml`; tag = `version` from
`.genvm-monorepo-root`, see [versioning.md](versioning.md)):

- `genvm-<os>-<arch>.tar.xz` ← `manager-<platform>`
- `genvm-<os>-<arch>-executor.tar.xz` ← `executor-<platform>`
- `genvm-runners-all.tar.xz` ← `runners-all-dist`

All three extract at the same install root.
