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

## Release assets

`.github/workflows/release.yaml` builds everything here — the executor is not a
standalone build entry point — then publishes each asset to the repo that owns
it.

Assets are named with the same platform token as the nix targets
(`amd64-linux`, …), not a reversed one.

**genvm-manager**, tagged with `version` from `.genvm-monorepo-root` (see
[versioning.md](versioning.md)):

- `genvm-<platform>.tar.xz` ← `manager-<platform>`
- `genvm-universal.tar.xz` ← `runners-all-dist` (platform-independent)

**genvm-executor**, one release per active line, tagged with that line's
`executor-version` from `executors/<line>.x/manifest.json`, created at the
commit the manager pins as its gitlink:

- `genvm-<platform>-executor.tar.xz` ← `executor-<line>-<platform>`

The manager release does not carry the executors. Assets still overlay onto one
install root, so a full install is the manager bundle + `genvm-universal` from
genvm-manager, plus each executor line from genvm-executor.

A line whose release tag already exists is skipped, not rebuilt — the legacy
v0.2 line usually does not move between manager releases.

Publishing into genvm-executor needs the `EXECUTOR_RELEASE_TOKEN` secret (a PAT
or GitHub App token with `contents: write` there); `GITHUB_TOKEN` cannot write
across repos.
