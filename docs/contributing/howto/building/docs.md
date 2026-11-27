# Docs

## Website

Sphinx, with its toolchain from the `gen-docs` nix shell — no poetry, no venv:

```bash
./support/ci/run.sh pipeline docs
```

The site lands in `build/doc/html/`; open its `index.html`.
Runner pages show their manifest and contents only for runners built into
`build/out/runners/` ([runners.md](runners.md)). Details:
`support/ci/pipelines/docs.py`

## Site Layout

Each site version directory (see Publishing) holds what its manager ships, and
its `index.html` lists them:

| Directory | Content | Source |
|---|---|---|
| `manager/` | overview, impl-spec | `docs/website/src/` |
| `executor/<version>/` | spec, Python SDK guides, runner table | `executors/<line>.x/docs/website/src/` |
| `runner/<id>/<hash>/` | 1 page per runner content hash | `docs/website/runner_pages.py` |

A runner page renders the runner's `runner.json` with each
dependency linked, and
`executors/<line>.x/runners/<dir>/docs/<id>.rst` when that exists, with an
optional `docs/conf.py` beside it; `docs/` sits outside the hashed sources

The manager, executor and runner pages reference each other through
intersphinx, as `:external+executor:`, `:external+manager:` and
`:external+sdk:`, so the pipeline builds them twice. `docs/website/common/`
holds the theme and settings all 3 share

Generated, never edited by hand:

| Page | Generator |
|---|---|
| executor `spec/appendix/constants.rst`, `impl-spec/appendix/manager-socket-consts.rst` | codegen ([genvm-tool.md](../genvm-tool.md)) |
| `available-runners.rst`, `reference.rst`, `runners-versions.json`, SDK `changelog.rst` | `docs/website/generate.py` |
| `overview/executor-lines_generated.rst` | `docs/website/generate.py` |

## Publishing

The site is `sdk.genlayer.com` — GitHub Pages out of
`genlayerlabs/sdk.genlayer.com`, one directory per version under `_site/` plus
`_site/versions.json` driving the version switcher; pushing that repo deploys
it

`.github/workflows/docs_nightly.yaml` publishes these versions:

| Trigger | Source | Site Directory |
|---|---|---|
| Successful release publication | Exact release tag | Exact release tag |
| Push to `v0.6` | Pushed commit | `v0.6.x` |
| Push to `v0.6-dev` | Pushed commit | `v0.6-dev` |
| Nightly schedule | `main` | `main` |
| Manual dispatch | Selected ref | `version` input (default `main`) |

The release workflow dispatches docs only after its final publishing job
succeeds, using `actions: write` on that dispatch job. A docs failure can be
retried by dispatching the docs workflow at the release tag with the same
`version`, without republishing the release

Concurrency is per target version and cancels the previous run for that version
Different versions may publish together: `deploy-docs --push` retries a rejected
push up to 5 attempts, each time reapplying the built tree and version entry to
the latest site `main`. This preserves concurrent changes to other versions and
`versions.json`; unchanged output produces no commit

The workflow needs `DOCS_DEPLOY_KEY` with write access to the site repo
Schedules and manual dispatch require the workflow on the repository's default
branch; push triggers require it on the pushed branch

By hand, into a checkout of the site repo:

```bash
./support/ci/run.sh pipeline docs
./support/ci/run.sh tool deploy-docs --site ../sdk.genlayer.com --version main
```

The version directory is replaced wholesale, so deleted pages do not linger;
`_site/versions.json` keeps its curated order, a missing version is inserted
after `main`. Committing and pushing the site repo is left to you unless you
pass `--push`, which requires a clean, disposable checkout and resets it to the
site repo's `origin/main` on each attempt

## ADRs

Numbered markdown in [`docs/adr/`](../../../adr/); start from
[`_template.md`](../../../adr/_template.md)
