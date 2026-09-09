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

`.github/workflows/docs_nightly.yaml` republishes `main` nightly and takes a
version/preferred pair on `workflow_dispatch` for a release train. It needs the
`DOCS_DEPLOY_KEY` secret, and — since GitHub schedules only from the default
branch — the workflow file must reach that branch before the cron can fire

By hand, into a checkout of the site repo:

```bash
./support/ci/run.sh pipeline docs
./support/ci/run.sh tool deploy-docs --site ../sdk.genlayer.com --version main
```

The version directory is replaced wholesale, so deleted pages do not linger;
`_site/versions.json` keeps its curated order, a missing version is inserted
after `main`. Committing and pushing the site repo is left to you

## ADRs

Numbered markdown in [`docs/adr/`](../../../adr/); start from
[`_template.md`](../../../adr/_template.md)
