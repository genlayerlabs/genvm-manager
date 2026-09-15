# Release Build

Nix flake packages see **committed** submodule content only, so commit inside
the submodules first ([submodules.md](../committing/submodules.md)), and always
pass `?submodules=1`:

```bash
nix build -v -L '.?submodules=1#genvm-amd64-linux'
```

Platform suffixes: none for native, `-amd64-linux`, `-arm64-linux`,
`-arm64-macos`

| Package | Contents |
|---|---|
| `genvm[-<platform>]` | everything: manager, every executor line, runners |
| `manager[-<platform>]` | the manager bundle: `bin/`, `lib/`, `config/`, `data/` |
| `executor[-<platform>]` | every active line merged, as `executor/<version>/…` |
| `executor-v0_3[-<platform>]` | one line; the dot in the line tag becomes an underscore |
| `artifact-prepack-genvm-<platform>` | a platform's release asset |
| `artifact-prepack-genvm-universal` | the universal asset: runners under a `runners/` prefix, plus the legacy-line overlay |

## Release Assets

`.github/workflows/release.yaml` is `incl_release_build_test.yaml` — plan, build
every asset, test every platform — plus a publisher. Everything is built there;
the executor is not a standalone build entry point

An asset is the prepacked tree itself, already named — nothing renames it:

| Asset | Package |
|---|---|
| `genvm-<platform>.tar.xz` | `artifact-prepack-genvm-<platform>` |
| `genvm-universal.tar.xz` | `artifact-prepack-genvm-universal` |

All of them go onto a single **genvm-manager** release, tagged with `version`
from `.genvm-monorepo-root` ([versioning.md](versioning.md)). They overlay onto
one install root, so a full install is one platform asset plus
`genvm-universal`. The platform asset already carries every active executor
line; post-install's executor download is only a fallback for a root missing
one ([versioning.md](versioning.md))

## Verifying an Asset

The publisher uploads `SHA256SUMS` next to the tarballs and records a keyless
sigstore provenance attestation for each of them, bound to `release.yaml` in
this repository at the commit the run was dispatched from (the tag is created
after the attestation, so it is not part of the identity). Verify a download
with either:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify genvm-amd64-linux.tar.xz --owner genlayerlabs
```

The second one is the stronger check: it proves the file came out of this
repository's release workflow, not merely that it matches a list published
alongside it

To exercise the whole pipeline on a PR without releasing anything, add the
`test-release-pipeline` label: `queue.yaml` then runs the same
`incl_release_build_test.yaml`, and prints the generated release notes so they
can be checked beforehand
