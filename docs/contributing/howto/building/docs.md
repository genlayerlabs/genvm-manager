# Docs

## Website

Sphinx, with its toolchain from the `gen-docs` nix shell — no poetry, no venv:

```bash
./support/ci/run.sh pipeline docs
```

That regenerates the derived pages, builds the manager site (`html` plus `text`
merged into `_static/ai/*.txt`), then each active line's sub-site in that
line's own `gen-docs` shell, into `build/doc/html/executors/<line>/`. Details:
`support/ci/pipelines/docs.py`. To iterate on sphinx alone:

```bash
nix develop '.?submodules=1#gen-docs' --command \
  sphinx-build -q -b dirhtml docs/website/src <out>
```

## Source Layout

Under `docs/website/src/`: `spec/` specifies observable behavior, `impl-spec/`
how this codebase does it, `overview/` is intro material. The Python SDK reference and
the runner-version pages live in the line that ships them,
`executors/<line>.x/docs/website/src/python-sdk/`. Cross-linking is one-way: a
sub-site references the manager through intersphinx, the manager only links out

Generated, never edited by hand:

| Page | Generator |
|---|---|
| `spec/appendix/constants.rst`, `impl-spec/appendix/manager-socket-consts.rst` | codegen ([genvm-tool.md](../genvm-tool.md)) |
| `available-runners.rst`, `runners-versions.json`, SDK `changelog.rst`, in the primary line | `docs/website/generate.py` |
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
